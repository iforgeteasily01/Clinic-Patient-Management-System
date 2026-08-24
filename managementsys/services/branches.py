"""Which branch a request is allowed to read, and which one it must write to.

Two different questions, deliberately answered by two different functions:

  * ``write_branch(request)`` — the single branch a new document is stamped
    with. Never negotiable for POS and medical records: those callers pass
    ``locked=True`` and get the user's ``home_branch`` no matter what the client
    sent. A cashier at Cabang B cannot ring up a sale against Cabang A's books
    by editing a header, because the header is not consulted.

  * ``read_branch_ids(request)`` — the set of branches a *query* may span. For
    the locked modules this is the home branch alone; for accounting and admin
    it is whatever the user selected, including "all branches" (returns None,
    meaning do not filter).

The client states its selection in the ``X-Branch-Id`` header, falling back to a
``?branch=`` query parameter so a link can carry it. ``all`` is a legal value for
the cross-branch roles only. Anything unparseable, inactive, or outside the
user's permission collapses silently to the home branch rather than erroring —
a stale tab must not be able to 400 the whole app, and the failure mode that
matters (writing to the wrong branch) is closed either way.

Nothing here trusts the client for authorisation. The header is a *preference*;
this module is where it becomes a permission.
"""
from ..models import AppUser, Branch

# Roles allowed to look at, and post to, a branch other than their own.
# Everyone else is confined to home_branch regardless of module.
CROSS_BRANCH_ROLES = frozenset({'superuser', 'manager'})

# The sentinel the client sends for "don't filter by branch".
ALL = 'all'


def can_cross_branch(user) -> bool:
    return isinstance(user, AppUser) and user.role in CROSS_BRANCH_ROLES


def home_branch(user):
    """The user's own location, falling back to the default branch.

    A user with no ``home_branch`` is not an error state — every user created
    before migration 0112 has none until an admin assigns one — so they land on
    the default branch rather than being locked out.
    """
    if isinstance(user, AppUser) and user.home_branch_id:
        return user.home_branch
    return Branch.get_default()


def _requested_raw(request):
    if request is None:
        return ''
    raw = request.headers.get('X-Branch-Id', '')
    if not raw:
        raw = request.query_params.get('branch', '') if hasattr(request, 'query_params') else ''
    return str(raw).strip().lower()


def _selectable(user):
    """Branches this user may choose between, active ones only.

    A caller with no ``AppUser`` at all is one of the legacy ``AllowAny``
    endpoints (the billing queue, the login user list). There is no identity to
    constrain, and branch here is a *filter* on an endpoint that was already
    open — narrowing it to the default branch would silently break the second
    clinic's till without closing anything. So they may name any active branch.
    Every endpoint where branch is a permission requires authentication.
    """
    qs = Branch.objects.filter(is_active=True)
    if can_cross_branch(user) or not isinstance(user, AppUser):
        return qs
    hb = home_branch(user)
    return qs.filter(pk=hb.pk) if hb else qs.none()


def resolve_selection(request):
    """(branch, is_all) for the client's stated selection, after permission.

    ``is_all`` is only ever True for a cross-branch role that explicitly asked
    for it; a locked role asking for 'all' gets its home branch instead.
    """
    user = getattr(request, 'user', None)
    raw = _requested_raw(request)

    if raw == ALL and can_cross_branch(user):
        return None, True

    if raw and raw != ALL:
        try:
            wanted = int(raw)
        except (TypeError, ValueError):
            wanted = None
        if wanted is not None:
            picked = _selectable(user).filter(pk=wanted).first()
            if picked is not None:
                return picked, False

    return home_branch(user), False


def _locked_branch(request):
    """The branch a locked module operates in.

    Normally the user's home branch. For an unauthenticated legacy endpoint
    there is no home branch to read, so the client's stated branch stands — see
    ``_selectable`` for why that is not a hole.
    """
    user = getattr(request, 'user', None)
    if isinstance(user, AppUser):
        return home_branch(user)
    branch, _ = resolve_selection(request)
    return branch


def write_branch(request, *, locked=False):
    """The branch a document created by this request belongs to.

    ``locked=True`` is for POS, the patient queue, treatment sessions and
    medical records — the modules whose branch is a fact about where the person
    physically is, not a choice. Those callers ignore the selection entirely.

    Accounting and admin callers pass ``locked=False`` and get the selection,
    which for a cross-branch role may be a branch other than their own. 'All
    branches' is not a place a document can live, so it also collapses to the
    home branch here — it is a read-side concept only.
    """
    user = getattr(request, 'user', None)
    if locked:
        return _locked_branch(request)
    branch, is_all = resolve_selection(request)
    return home_branch(user) if is_all else branch


def read_branch_ids(request, *, locked=False):
    """Branch ids a query may span, or None for 'every branch'.

    None is the caller's cue to skip the filter entirely rather than to build
    an ``id__in`` over every branch — the two differ for rows whose branch is
    null (group-wide overhead), which 'all branches' must still include.
    """
    if locked:
        hb = _locked_branch(request)
        return [hb.pk] if hb else []
    branch, is_all = resolve_selection(request)
    if is_all:
        return None
    return [branch.pk] if branch else []


def filter_by_branch(queryset, request, *, field='branch', locked=False,
                     include_null=True):
    """Apply the request's branch selection to ``queryset``.

    ``include_null`` keeps rows with no branch visible when a specific branch is
    selected. That is the right default for the ledger: an entry booked before
    migration 0112, or a genuinely group-wide one, would otherwise vanish from
    every branch's view and the branch P&Ls would not sum to the group P&L.
    Pass ``include_null=False`` for operational lists (the queue, POS) where a
    null branch is legacy noise rather than shared cost.
    """
    ids = read_branch_ids(request, locked=locked)
    if ids is None:
        return queryset
    lookup = {f'{field}_id__in': ids}
    qs = queryset.filter(**lookup)
    if include_null:
        from django.db.models import Q
        qs = queryset.filter(Q(**lookup) | Q(**{f'{field}_id__isnull': True}))
    return qs


def receipt_identity(branch, site_config):
    """Receipt header lines: branch overrides location, SiteConfig owns identity.

    A group prints one legal name and one logo across every location, but the
    address and phone on the receipt have to be the branch the patient actually
    visited. A blank branch field falls through to SiteConfig so a single-branch
    clinic never has to fill the same three fields in twice.
    """
    def pick(attr):
        val = getattr(branch, attr, '') if branch is not None else ''
        return val or getattr(site_config, attr, '')

    return {
        'clinic_name':   getattr(site_config, 'clinic_name', ''),
        'branch_name':   getattr(branch, 'name', '') if branch is not None else '',
        'address_line1': pick('address_line1'),
        'address_line2': pick('address_line2'),
        'phone_fax':     pick('phone_fax'),
    }
