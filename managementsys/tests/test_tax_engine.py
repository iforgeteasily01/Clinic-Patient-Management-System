"""Tax rule evaluation tests.

Every assertion here is a hand-computable figure. A tax engine that agrees with
itself is worthless — the point of these numbers is that they were worked out
from the statute, not from a previous run of this code.
"""
import datetime
from decimal import Decimal

import pytest

from managementsys.models import (
    JournalDayLog, LedgerEntry, TaxRule, TaxRuleBracket, TaxRuleComponent,
)
from managementsys.services.tax_engine import (
    TaxRuleError, compute, order_rules, rule_window,
)
from .factories import ChartOfAccountsFactory

JAN = datetime.date(2026, 1, 15)
JUN = datetime.date(2026, 6, 15)
FROM = datetime.date(2026, 6, 1)
TO = datetime.date(2026, 6, 30)


def _le(account, entry_type, amount, when):
    LedgerEntry.objects.create(
        account=account, date=when, description='test',
        entry_type=entry_type, amount=Decimal(amount), source_type='manual',
    )


@pytest.fixture
def books(db):
    """Two revenue accounts and one expense account, posted in Jan and Jun.

    June alone: revenue 100.000.000, expense 40.000.000.
    Year to date: revenue 150.000.000, expense 60.000.000.
    """
    accounts = {
        'cash':    ChartOfAccountsFactory(account_number=1100000, name='Kas', account_type='asset'),
        'service': ChartOfAccountsFactory(account_number=4400000, name='Jasa', account_type='revenue'),
        'product': ChartOfAccountsFactory(account_number=4200000, name='Produk', account_type='revenue'),
        'salary':  ChartOfAccountsFactory(account_number=6400010, name='Gaji', account_type='expense'),
    }
    # January
    _le(accounts['cash'], 'debit', 50_000_000, JAN)
    _le(accounts['service'], 'credit', 50_000_000, JAN)
    _le(accounts['salary'], 'debit', 20_000_000, JAN)
    _le(accounts['cash'], 'credit', 20_000_000, JAN)
    # June
    _le(accounts['cash'], 'debit', 60_000_000, JUN)
    _le(accounts['service'], 'credit', 60_000_000, JUN)
    _le(accounts['cash'], 'debit', 40_000_000, JUN)
    _le(accounts['product'], 'credit', 40_000_000, JUN)
    _le(accounts['salary'], 'debit', 40_000_000, JUN)
    _le(accounts['cash'], 'credit', 40_000_000, JUN)
    return accounts


def _rule(code, **kwargs):
    kwargs.setdefault('name', code)
    return TaxRule.objects.create(code=code, **kwargs)


def _component(rule, **kwargs):
    kwargs.setdefault('source', 'account')
    kwargs.setdefault('sign', 1)
    return TaxRuleComponent.objects.create(rule=rule, **kwargs)


def _by_code(rows):
    return {r['code']: r for r in rows}


# ── base construction ────────────────────────────────────────────────────────

def test_flat_rate_over_account_type(books):
    """11% of June revenue (100.000.000) = 11.000.000."""
    rule = _rule('ppn', rate_mode='flat', rate_percent=Decimal('11'))
    _component(rule, source='type', account_type='revenue')

    row = _by_code(compute(FROM, TO))['ppn']
    assert row['base'] == Decimal('100000000')
    assert row['result'] == Decimal('11000000')


def test_signed_components_net_against_each_other(books):
    """Revenue 100.000.000 less expense 40.000.000 = 60.000.000."""
    rule = _rule('laba', rate_mode='none')
    _component(rule, source='type', account_type='revenue', sign=1)
    _component(rule, source='type', account_type='expense', sign=-1)

    assert _by_code(compute(FROM, TO))['laba']['base'] == Decimal('60000000')


def test_single_account_component_ignores_siblings(books):
    """Product revenue only — the service account must not leak in."""
    rule = _rule('ppn_produk', rate_mode='flat', rate_percent=Decimal('11'))
    _component(rule, source='account', account=books['product'])

    row = _by_code(compute(FROM, TO))['ppn_produk']
    assert row['base'] == Decimal('40000000')
    assert row['result'] == Decimal('4400000')


def test_subtree_component_includes_children(books):
    """A subtree component must pick up accounts filed under the parent."""
    head = ChartOfAccountsFactory(
        account_number=4000000, name='Pendapatan', account_type='revenue', is_head=True)
    books['service'].parent = head
    books['service'].save()
    books['product'].parent = head
    books['product'].save()

    rule = _rule('omzet', rate_mode='none')
    _component(rule, source='subtree', account=head)

    assert _by_code(compute(FROM, TO))['omzet']['base'] == Decimal('100000000')


def test_fixed_component_adds_a_constant(books):
    rule = _rule('konstanta', rate_mode='none')
    _component(rule, source='fixed', fixed_amount=Decimal('2500000'))

    assert _by_code(compute(FROM, TO))['konstanta']['base'] == Decimal('2500000')


# ── window ───────────────────────────────────────────────────────────────────

def test_ytd_basis_widens_window_to_january(books):
    """A 'ytd' rule reads from 1 Jan regardless of the requested date_from:
    150.000.000, not June's 100.000.000."""
    rule = _rule('omzet_ytd', basis='ytd', rate_mode='none')
    _component(rule, source='type', account_type='revenue')

    row = _by_code(compute(FROM, TO))['omzet_ytd']
    assert row['base'] == Decimal('150000000')
    assert row['window']['date_from'] == '2026-01-01'


def test_rule_window_leaves_period_basis_alone():
    rule = TaxRule(code='x', basis='period')
    assert rule_window(rule, FROM, TO) == (FROM, TO)


# ── deduction ────────────────────────────────────────────────────────────────

def test_deduction_reduces_base(books):
    rule = _rule('setelah_ptkp', rate_mode='none',
                 deduction_amount=Decimal('30000000'))
    _component(rule, source='type', account_type='revenue')

    row = _by_code(compute(FROM, TO))['setelah_ptkp']
    assert row['gross_base'] == Decimal('100000000')
    assert row['base'] == Decimal('70000000')


def test_deduction_floors_at_zero_rather_than_inverting(books):
    """PTKP above gross pay means no PPh 21 — never a negative tax."""
    rule = _rule('ptkp_besar', rate_mode='flat', rate_percent=Decimal('5'),
                 deduction_amount=Decimal('500000000'))
    _component(rule, source='type', account_type='revenue')

    row = _by_code(compute(FROM, TO))['ptkp_besar']
    assert row['base'] == Decimal('0')
    assert row['result'] == Decimal('0')


# ── progressive brackets ─────────────────────────────────────────────────────

def test_brackets_tax_each_layer_at_its_own_rate(books):
    """Base 100.000.000 against the UU HPP layers:
        first  60.000.000 @  5% =  3.000.000
        next   40.000.000 @ 15% =  6.000.000
                                  ──────────
                                   9.000.000
    A bracket rate must never apply to the whole base.
    """
    rule = _rule('pph21', rate_mode='bracket', rounding='none')
    _component(rule, source='type', account_type='revenue')
    TaxRuleBracket.objects.create(rule=rule, upper_bound=Decimal('60000000'),
                                  rate_percent=Decimal('5'), display_order=0)
    TaxRuleBracket.objects.create(rule=rule, upper_bound=Decimal('250000000'),
                                  rate_percent=Decimal('15'), display_order=1)
    TaxRuleBracket.objects.create(rule=rule, upper_bound=None,
                                  rate_percent=Decimal('35'), display_order=2)

    row = _by_code(compute(FROM, TO))['pph21']
    assert row['result'] == Decimal('9000000')
    layers = row['rate_detail']['brackets']
    assert [l['amount'] for l in layers] == [Decimal('60000000'), Decimal('40000000')]


def test_open_ended_bracket_absorbs_the_remainder(books):
    """Base 100.000.000, single layer to 60.000.000 @ 10%, rest open @ 20%:
    6.000.000 + 8.000.000 = 14.000.000."""
    rule = _rule('progresif', rate_mode='bracket', rounding='none')
    _component(rule, source='type', account_type='revenue')
    TaxRuleBracket.objects.create(rule=rule, upper_bound=Decimal('60000000'),
                                  rate_percent=Decimal('10'), display_order=0)
    TaxRuleBracket.objects.create(rule=rule, upper_bound=None,
                                  rate_percent=Decimal('20'), display_order=1)

    assert _by_code(compute(FROM, TO))['progresif']['result'] == Decimal('14000000')


# ── Pasal 31E facility ───────────────────────────────────────────────────────

def _facility_pair(turnover_amount, base_amount, **facility):
    """A turnover rule and a facility rule reading a fixed base, so the 31E
    arithmetic can be checked without staging ledger rows for each case."""
    turnover = _rule('omzet', rate_mode='none', rounding='none')
    TaxRuleComponent.objects.create(
        rule=turnover, source='fixed', sign=1, fixed_amount=Decimal(turnover_amount))

    badan = _rule('pph_badan', rate_mode='facility', rate_percent=Decimal('22'),
                  rounding='none', facility_turnover_rule=turnover,
                  facility_turnover_cap=Decimal('4800000000'),
                  facility_full_rate_cap=Decimal('50000000000'),
                  facility_factor=Decimal('0.5'), display_order=1, **facility)
    TaxRuleComponent.objects.create(
        rule=badan, source='fixed', sign=1, fixed_amount=Decimal(base_amount))
    return badan


def test_facility_below_cap_discounts_whole_base(db):
    """Turnover under Rp 4,8 M: every rupiah of profit at 22% x 50% = 11%.
    1.000.000.000 x 11% = 110.000.000."""
    _facility_pair('4000000000', '1000000000')
    assert _by_code(compute(FROM, TO))['pph_badan']['result'] == Decimal('110000000')


def test_facility_above_full_cap_charges_plain_rate(db):
    """Turnover over Rp 50 M: no facility at all. 1.000.000.000 x 22%."""
    _facility_pair('60000000000', '1000000000')
    assert _by_code(compute(FROM, TO))['pph_badan']['result'] == Decimal('220000000')


def test_facility_middle_band_is_proportional(db):
    """Turnover 9.600.000.000 — exactly twice the cap — so half the profit is
    facilitated:
        facilitated  500.000.000 @ 11% =  55.000.000
        remainder    500.000.000 @ 22% = 110.000.000
                                         ───────────
                                          165.000.000
    """
    _facility_pair('9600000000', '1000000000')
    row = _by_code(compute(FROM, TO))['pph_badan']
    assert row['rate_detail']['facilitated_base'] == Decimal('500000000')
    assert row['result'] == Decimal('165000000')


def test_facility_without_turnover_reference_charges_full_rate(db):
    """No turnover to test against must not silently grant the discount."""
    rule = _rule('badan_tanpa_omzet', rate_mode='facility',
                 rate_percent=Decimal('22'), rounding='none',
                 facility_turnover_cap=Decimal('4800000000'),
                 facility_factor=Decimal('0.5'))
    _component(rule, source='fixed', fixed_amount=Decimal('1000000000'))

    assert _by_code(compute(FROM, TO))['badan_tanpa_omzet']['result'] == Decimal('220000000')


# ── rule references ──────────────────────────────────────────────────────────

def test_rule_component_reads_another_rules_result(books):
    """PPN Kurang Bayar = keluaran - masukan."""
    keluaran = _rule('keluaran', rate_mode='flat', rate_percent=Decimal('11'),
                     display_order=0)
    _component(keluaran, source='type', account_type='revenue')
    masukan = _rule('masukan', rate_mode='none', display_order=1)
    _component(masukan, source='fixed', fixed_amount=Decimal('4000000'))
    net = _rule('kurang_bayar', rate_mode='none', display_order=2)
    _component(net, source='rule', source_rule=keluaran, sign=1)
    _component(net, source='rule', source_rule=masukan, sign=-1)

    rows = _by_code(compute(FROM, TO))
    assert rows['keluaran']['result'] == Decimal('11000000')
    assert rows['kurang_bayar']['result'] == Decimal('7000000')


def test_dependency_order_is_independent_of_display_order(books):
    """A rule declared first but depending on a later one still evaluates
    after it — display order must not decide evaluation order."""
    net = _rule('net', rate_mode='none', display_order=0)
    source = _rule('source', rate_mode='none', display_order=9)
    _component(source, source='fixed', fixed_amount=Decimal('5000000'))
    _component(net, source='rule', source_rule=source)

    ordered = [r.code for r in order_rules(list(TaxRule.objects.all()))]
    assert ordered.index('source') < ordered.index('net')
    assert _by_code(compute(FROM, TO))['net']['result'] == Decimal('5000000')


def test_circular_reference_is_reported_not_hung(db):
    a = _rule('a', rate_mode='none')
    b = _rule('b', rate_mode='none')
    _component(a, source='rule', source_rule=b)
    _component(b, source='rule', source_rule=a)

    with pytest.raises(TaxRuleError, match='saling merujuk'):
        compute(FROM, TO)


# ── rounding ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('mode,expected', [
    ('none',     Decimal('1234567.89')),
    ('rupiah',   Decimal('1234567')),
    ('thousand', Decimal('1234000')),
])
def test_rounding_always_goes_down(db, mode, expected):
    rule = _rule('bulat', rate_mode='none', rounding=mode)
    _component(rule, source='fixed', fixed_amount=Decimal('1234567.89'))

    assert _by_code(compute(FROM, TO))['bulat']['result'] == expected


def test_rounding_of_a_refund_position_shrinks_the_magnitude(db):
    """A lebih-bayar (negative) figure must round toward zero, not away:
    -1.500 rounds to -1.000, never -2.000."""
    rule = _rule('lebih_bayar', rate_mode='none', rounding='thousand')
    _component(rule, source='fixed', fixed_amount=Decimal('-1500'))

    assert _by_code(compute(FROM, TO))['lebih_bayar']['result'] == Decimal('-1000')


# ── activation ───────────────────────────────────────────────────────────────

def test_inactive_rules_are_skipped(books):
    rule = _rule('nonaktif', rate_mode='flat', rate_percent=Decimal('11'),
                 is_active=False)
    _component(rule, source='type', account_type='revenue')

    assert 'nonaktif' not in _by_code(compute(FROM, TO))


def test_rule_outside_its_effective_window_is_skipped(books):
    """A rate that ended before the period must not be applied to it."""
    rule = _rule('tarif_lama', rate_mode='flat', rate_percent=Decimal('10'),
                 effective_to=datetime.date(2026, 3, 31))
    _component(rule, source='type', account_type='revenue')

    assert 'tarif_lama' not in _by_code(compute(FROM, TO))


# ── the report gate ──────────────────────────────────────────────────────────

def test_compute_endpoint_refuses_an_unswept_period(books, client):
    """Same guard as the financial reports: an unposted day means the figures
    are incomplete, and a tax number that reads low is worse than none."""
    from rest_framework.test import APIRequestFactory, force_authenticate
    from managementsys.views.tax_page import TaxComputeView
    from .factories import AppUserFactory

    JournalDayLog.objects.create(date=FROM, is_posted=True)  # only one day posted

    request = APIRequestFactory().get(
        '/api/accounting/tax/compute/',
        {'date_from': FROM.isoformat(), 'date_to': TO.isoformat()})
    force_authenticate(request, user=AppUserFactory())
    response = TaxComputeView.as_view()(request)

    assert response.status_code == 400
    assert 'unposted_dates' in response.data
