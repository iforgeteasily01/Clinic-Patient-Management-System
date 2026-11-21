from ..models import AppUser, Branch

CROSS_BRANCH_ROLES = frozenset({'superuser', 'manager'})

ALL = 'all'


def can_cross_branch(user) -> bool:
    return isinstance(user, AppUser) and user.role in CROSS_BRANCH_ROLES


def home_branch(user):
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
    qs = Branch.objects.filter(is_active=True)
    if can_cross_branch(user) or not isinstance(user, AppUser):
        return qs
    hb = home_branch(user)
    return qs.filter(pk=hb.pk) if hb else qs.none()


def resolve_selection(request):
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
    user = getattr(request, 'user', None)
    if isinstance(user, AppUser):
        return home_branch(user)
    branch, _ = resolve_selection(request)
    return branch


def write_branch(request, *, locked=False):
    user = getattr(request, 'user', None)
    if locked:
        return _locked_branch(request)
    branch, is_all = resolve_selection(request)
    return home_branch(user) if is_all else branch


def read_branch_ids(request, *, locked=False):
    if locked:
        hb = _locked_branch(request)
        return [hb.pk] if hb else []
    branch, is_all = resolve_selection(request)
    if is_all:
        return None
    return [branch.pk] if branch else []


def filter_by_branch(queryset, request, *, field='branch', locked=False,
                     include_null=True):
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
