"""Branch scoping: what a request may write to, and what it may read.

The rules under test are the ones a bug would be expensive in:

  * A locked module (POS, the queue, medical records) ignores the branch header
    completely. A cashier cannot book a sale into another clinic's books by
    editing a request header.
  * A cross-branch role gets what it asked for; a locked role silently falls
    back to its own branch rather than erroring, because a stale browser tab
    must not be able to 400 the whole app.
  * 'All branches' is a read-side concept only. Nothing can be *written* to it.
  * A branch-scoped ledger query keeps null-branch rows, so per-branch reports
    still sum to the group report.
"""
from decimal import Decimal

import pytest

from managementsys.models import Branch, ChartOfAccounts, LedgerEntry
from managementsys.services import branches as svc

from .factories import AppUserFactory, BranchFactory


class FakeRequest:
    """Minimal stand-in for a DRF request: a user, headers and query params."""

    def __init__(self, user=None, header=None, param=None):
        self.user = user
        self.headers = {'X-Branch-Id': str(header)} if header is not None else {}
        self.query_params = {'branch': str(param)} if param is not None else {}


@pytest.fixture
def two_branches(db):
    home = BranchFactory(code='HOME', name='Klinik Pusat', is_default=True)
    other = BranchFactory(code='OTHER', name='Klinik Cabang')
    return home, other


# ── write_branch ──────────────────────────────────────────────────────────────

def test_locked_module_ignores_the_branch_header(two_branches):
    home, other = two_branches
    cashier = AppUserFactory(role='cashier', home_branch=home)

    req = FakeRequest(user=cashier, header=other.pk)

    assert svc.write_branch(req, locked=True) == home


def test_locked_module_ignores_the_header_even_for_a_manager(two_branches):
    """Cross-branch is an accounting permission, not a licence to ring up
    sales at another clinic's till."""
    home, other = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    req = FakeRequest(user=manager, header=other.pk)

    assert svc.write_branch(req, locked=True) == home


def test_manager_writes_accounting_documents_to_the_selected_branch(two_branches):
    home, other = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    req = FakeRequest(user=manager, header=other.pk)

    assert svc.write_branch(req) == other


def test_cashier_asking_for_another_branch_falls_back_to_their_own(two_branches):
    home, other = two_branches
    cashier = AppUserFactory(role='cashier', home_branch=home)

    req = FakeRequest(user=cashier, header=other.pk)

    assert svc.write_branch(req) == home


def test_all_branches_is_not_a_place_a_document_can_live(two_branches):
    home, _ = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    req = FakeRequest(user=manager, header='all')

    assert svc.write_branch(req) == home


def test_garbage_header_falls_back_instead_of_raising(two_branches):
    home, _ = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    assert svc.write_branch(FakeRequest(user=manager, header='not-a-number')) == home


def test_query_parameter_works_when_no_header_is_sent(two_branches):
    """So a link can carry the branch."""
    home, other = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    assert svc.write_branch(FakeRequest(user=manager, param=other.pk)) == other


def test_user_with_no_home_branch_lands_on_the_default(two_branches):
    home, _ = two_branches
    orphan = AppUserFactory(role='cashier', home_branch=None)

    assert svc.home_branch(orphan) == home


def test_inactive_branch_cannot_be_selected(two_branches):
    home, other = two_branches
    other.is_active = False
    other.save()
    manager = AppUserFactory(role='manager', home_branch=home)

    assert svc.write_branch(FakeRequest(user=manager, header=other.pk)) == home


# ── read_branch_ids ───────────────────────────────────────────────────────────

def test_all_branches_reads_as_no_filter_not_as_every_id(two_branches):
    """None and [every id] are not interchangeable: only None keeps rows whose
    branch is null."""
    home, _ = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    assert svc.read_branch_ids(FakeRequest(user=manager, header='all')) is None


def test_locked_read_is_the_home_branch_alone(two_branches):
    home, other = two_branches
    doctor = AppUserFactory(role='doctor', home_branch=home)

    ids = svc.read_branch_ids(FakeRequest(user=doctor, header=other.pk), locked=True)

    assert ids == [home.pk]


def test_a_locked_role_asking_for_all_gets_its_own_branch(two_branches):
    home, _ = two_branches
    cashier = AppUserFactory(role='cashier', home_branch=home)

    assert svc.read_branch_ids(FakeRequest(user=cashier, header='all')) == [home.pk]


# ── filter_by_branch ──────────────────────────────────────────────────────────

@pytest.fixture
def ledger_rows(two_branches):
    """One entry per branch plus one with no branch at all (group-wide)."""
    home, other = two_branches
    account = ChartOfAccounts.objects.create(
        account_number=6100001, name='Beban Uji', account_type='expense',
    )
    common = dict(account=account, date='2026-01-05', entry_type='debit',
                  amount=Decimal('1000'), source_type='manual')
    return {
        'home':  LedgerEntry.objects.create(description='home', branch=home, **common),
        'other': LedgerEntry.objects.create(description='other', branch=other, **common),
        'none':  LedgerEntry.objects.create(description='none', branch=None, **common),
    }


def test_selecting_a_branch_keeps_null_branch_rows(two_branches, ledger_rows):
    """Group-wide overhead and pre-migration history must stay visible, or the
    branch P&Ls stop summing to the group P&L."""
    home, _ = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    qs = svc.filter_by_branch(LedgerEntry.objects.all(),
                              FakeRequest(user=manager, header=home.pk))

    assert {e.description for e in qs} == {'home', 'none'}


def test_operational_lists_can_opt_out_of_null_rows(two_branches, ledger_rows):
    home, _ = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    qs = svc.filter_by_branch(LedgerEntry.objects.all(),
                              FakeRequest(user=manager, header=home.pk),
                              include_null=False)

    assert {e.description for e in qs} == {'home'}


def test_all_branches_returns_everything_including_unbranched(two_branches, ledger_rows):
    home, _ = two_branches
    manager = AppUserFactory(role='manager', home_branch=home)

    qs = svc.filter_by_branch(LedgerEntry.objects.all(),
                              FakeRequest(user=manager, header='all'))

    assert {e.description for e in qs} == {'home', 'other', 'none'}


# ── Branch model ──────────────────────────────────────────────────────────────

def test_promoting_a_default_demotes_the_previous_one(two_branches):
    home, other = two_branches
    other.is_default = True
    other.save()

    home.refresh_from_db()
    assert not home.is_default
    assert Branch.objects.filter(is_default=True).count() == 1


def test_get_default_falls_back_when_no_row_is_flagged(db):
    a = BranchFactory(code='A', sort_order=1)
    BranchFactory(code='B', sort_order=2)
    Branch.objects.update(is_default=False)

    assert Branch.get_default() == a


# ── receipt_identity ──────────────────────────────────────────────────────────

def test_receipt_falls_back_to_site_config_for_blank_branch_fields(two_branches):
    from managementsys.models import SiteConfig

    home, _ = two_branches
    cfg = SiteConfig.get_solo()
    cfg.clinic_name = 'Klinik Group'
    cfg.address_line1 = 'Jl. Pusat 1'
    cfg.phone_fax = '021-000'
    cfg.save()

    home.address_line1 = 'Jl. Cabang 9'
    home.phone_fax = ''
    home.save()

    out = svc.receipt_identity(home, cfg)

    # The legal name is always the group's; the location lines are the branch's
    # where it has them and the group's where it does not.
    assert out['clinic_name'] == 'Klinik Group'
    assert out['branch_name'] == home.name
    assert out['address_line1'] == 'Jl. Cabang 9'
    assert out['phone_fax'] == '021-000'
