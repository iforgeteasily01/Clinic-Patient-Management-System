"""Financial statement endpoint tests (Laba Rugi, Neraca, Neraca Saldo,
Buku Besar, Arus Kas).

A small balanced set of LedgerEntry rows is posted, then every report is
exercised through its APIView and the derived totals are asserted.
"""
import datetime
import itertools
from decimal import Decimal

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from managementsys.models import JournalDayLog, JournalEntry, LedgerEntry
from managementsys.views.financial_reports_page import (
    BalanceSheetView,
    CashFlowView,
    GeneralLedgerView,
    ProfitLossView,
    TrialBalanceView,
)
from .factories import AppUserFactory, ChartOfAccountsFactory

D1 = datetime.date(2026, 1, 10)   # capital injection
D2 = datetime.date(2026, 1, 20)   # sale + expense
FROM = '2026-01-01'
TO = '2026-01-31'


def _mark_range_posted(date_from, date_to):
    """Simulate a journal run's JournalDayLog side effect: every calendar day
    in the range is marked posted, not just the days with transactions — a
    real run does the same (it sweeps the whole span, not just active days)."""
    cur = date_from
    while cur <= date_to:
        JournalDayLog.objects.get_or_create(date=cur, defaults={'is_posted': True})
        cur += datetime.timedelta(days=1)


def _le(account, entry_type, amount, when, source='manual', journal_entry=None):
    LedgerEntry.objects.create(
        account=account, date=when, description='test',
        entry_type=entry_type, amount=Decimal(amount), source_type=source,
        journal_entry=journal_entry,
    )


_je_seq = itertools.count(1)


def _post(when, source, *legs):
    """Post one balanced journal entry. ``legs`` are ``(account, entry_type,
    amount)`` triples.

    The cash-flow statement attributes a cash movement through the *other*
    lines of its own JournalEntry, so a test that writes bare LedgerEntry rows
    with no header exercises only the "cannot classify" path. Everything that
    asserts on an activity section must post through here.
    """
    je = JournalEntry.objects.create(
        entry_number=f'TEST-{next(_je_seq):05d}', date=when, source_type=source,
        total_debit=sum((Decimal(a) for _, t, a in legs if t == 'debit'), Decimal(0)),
        total_credit=sum((Decimal(a) for _, t, a in legs if t == 'credit'), Decimal(0)),
    )
    for account, entry_type, amount in legs:
        _le(account, entry_type, amount, when, source, journal_entry=je)
    return je


@pytest.fixture
def books(db):
    # 1100001, not 1100000: the latter is the *head* of the cash band and, like
    # every head, never carries entries. Building the fixture on the head is
    # what let the cash-flow report ship reading an account set that could not
    # match a single ledger row.
    cash = ChartOfAccountsFactory(account_number=1100001, name='Cash on Hand', account_type='asset')
    revenue = ChartOfAccountsFactory(account_number=4100000, name='Treatment Revenue', account_type='revenue')
    expense = ChartOfAccountsFactory(account_number=6100000, name='Salaries', account_type='expense')
    equity = ChartOfAccountsFactory(account_number=3100000, name="Owner's Capital", account_type='equity')

    _post(D1, 'adjustment',                             # capital in
          (cash, 'debit', 1_000_000), (equity, 'credit', 1_000_000))
    _post(D2, 'invoice',                                # sale
          (cash, 'debit', 500_000), (revenue, 'credit', 500_000))
    _post(D2, 'adjustment',                             # expense paid cash
          (expense, 'debit', 200_000), (cash, 'credit', 200_000))

    # Phase 2: every report now refuses to compute over a range with any
    # unposted calendar day. These LedgerEntry rows are written directly
    # (bypassing the journal run, same as a manual JournalAdjustmentView
    # entry), so the whole reporting window is marked posted here — mirroring
    # what a journal run covering FROM..TO would have done.
    _mark_range_posted(datetime.date.fromisoformat(FROM), datetime.date.fromisoformat(TO))
    return {'cash': cash, 'revenue': revenue, 'expense': expense, 'equity': equity}


def _call(view, **params):
    req = APIRequestFactory().get('/x/', params)
    force_authenticate(req, user=AppUserFactory(pin='654321'))
    return view.as_view()(req)


def test_trial_balance_balances(books):
    d = _call(TrialBalanceView, as_of=TO).data
    assert d['is_balanced'] is True
    assert Decimal(d['total_debit']) == Decimal(d['total_credit'])
    # cash net 1.3M debit + expense 0.2M debit vs equity 1.0M + revenue 0.5M credit
    assert Decimal(d['total_debit']) == Decimal('1500000')


def test_profit_loss(books):
    d = _call(ProfitLossView, date_from=FROM, date_to=TO).data
    assert Decimal(d['revenue']['total']) == Decimal('500000')
    assert Decimal(d['operating_expenses']['total']) == Decimal('200000')
    assert Decimal(d['gross_profit']) == Decimal('500000')
    assert Decimal(d['net_profit']) == Decimal('300000')


def test_balance_sheet_balances(books):
    d = _call(BalanceSheetView, as_of=TO).data
    assert Decimal(d['total_assets']) == Decimal('1300000')
    assert Decimal(d['equity']['retained_earnings']) == Decimal('300000')
    assert Decimal(d['total_assets']) == Decimal(d['total_liabilities_and_equity'])
    assert d['is_balanced'] is True


def _activity(d, name):
    return next(a for a in d['activities'] if a['activity'] == name)


def test_cash_flow(books):
    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert Decimal(d['opening_cash']) == Decimal('0')
    assert Decimal(d['closing_cash']) == Decimal('1300000')
    assert Decimal(d['net_change']) == Decimal('1300000')


def test_cash_flow_reports_the_cash_band_not_the_head(books):
    """The regression this whole report used to fail on: cash was looked up by
    the head account number 1100000, which never carries a ledger row, so every
    period came back with no flows and a zero closing balance."""
    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert [a['account_number'] for a in d['accounts']] == [1100001]
    assert Decimal(d['accounts'][0]['closing']) == Decimal('1300000')
    assert Decimal(d['accounts'][0]['inflow']) == Decimal('1500000')
    assert Decimal(d['accounts'][0]['outflow']) == Decimal('200000')


def test_cash_flow_splits_operating_from_financing(books):
    """The 1.0M capital injection is financing (equity counterpart); the 0.5M
    sale and the 0.2M expense are operating. `source_type` alone cannot make
    this split - both cash legs were posted as 'adjustment'."""
    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert Decimal(_activity(d, 'financing')['net']) == Decimal('1000000')
    assert Decimal(_activity(d, 'operating')['net']) == Decimal('300000')
    assert Decimal(_activity(d, 'investing')['net']) == Decimal('0')

    operating = {l['account_number']: Decimal(l['net']) for l in _activity(d, 'operating')['lines']}
    assert operating[4100000] == Decimal('500000')    # revenue in
    assert operating[6100000] == Decimal('-200000')   # expense out


def test_cash_flow_nets_out_internal_transfers(books):
    """Moving money between two cash accounts is neither an inflow nor an
    outflow - counting both legs would inflate gross cash movement while
    leaving the net unchanged, which is the misleading half."""
    bank = ChartOfAccountsFactory(account_number=1100002, name='Bank', account_type='asset')
    _post(D2, 'transfer', (bank, 'debit', 400_000), (books['cash'], 'credit', 400_000))

    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert Decimal(d['net_change']) == Decimal('1300000')   # unchanged
    assert Decimal(d['closing_cash']) == Decimal('1300000')
    assert Decimal(d['internal_transfers']['inflow']) == Decimal('400000')
    assert Decimal(d['internal_transfers']['net']) == Decimal('0')
    # ...but each account's own balance did move.
    per = {a['account_number']: a for a in d['accounts']}
    assert Decimal(per[1100002]['closing']) == Decimal('400000')
    assert Decimal(per[1100001]['closing']) == Decimal('900000')
    # and no activity section absorbed it
    assert sum(Decimal(a['net']) for a in d['activities']) == Decimal('1300000')


def test_cash_flow_attributes_contra_accounts_to_the_correct_side(books):
    """A sales discount is debited inside an otherwise credit-side sale. It has
    to land as an outflow; weighting counterparts by raw amount instead of by
    signed side would report it as an inflow."""
    discount = ChartOfAccountsFactory(account_number=4900000, name='Sales Discount', account_type='revenue')
    _post(D2, 'invoice',
          (books['cash'], 'debit', 90_000),
          (discount, 'debit', 10_000),
          (books['revenue'], 'credit', 100_000))

    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    lines = {l['account_number']: Decimal(l['net']) for l in _activity(d, 'operating')['lines']}
    assert lines[4900000] == Decimal('-10000')
    assert lines[4100000] == Decimal('600000')      # 500k + the new 100k
    assert Decimal(d['net_change']) == Decimal('1390000')


def test_cash_flow_classifies_fixed_asset_purchase_as_investing(books):
    equipment = ChartOfAccountsFactory(account_number=1500000, name='Equipment', account_type='asset')
    _post(D2, 'purchase', (equipment, 'debit', 250_000), (books['cash'], 'credit', 250_000))

    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert Decimal(_activity(d, 'investing')['net']) == Decimal('-250000')
    assert Decimal(_activity(d, 'operating')['net']) == Decimal('300000')


def test_cash_flow_surfaces_unbalanced_entries_instead_of_hiding_them(books):
    """A one-sided journal entry (a real bug seen in production purchase
    payments, which credited bank without debiting accounts payable) has no
    counterpart to attribute through. It belongs in its own bucket - folding it
    into operating would let a posting bug pass as a plausible subtotal."""
    je = JournalEntry.objects.create(
        entry_number='TEST-ONESIDED', date=D2, source_type='purchase',
        total_credit=Decimal('75000'), is_balanced=False)
    _le(books['cash'], 'credit', 75_000, D2, 'purchase', journal_entry=je)

    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert Decimal(d['unclassified']['net']) == Decimal('-75000')
    assert [l['source_type'] for l in d['unclassified']['lines']] == ['purchase']
    assert Decimal(_activity(d, 'operating')['net']) == Decimal('300000')
    # the statement still foots
    total = (sum(Decimal(a['net']) for a in d['activities'])
             + Decimal(d['unclassified']['net'])
             + Decimal(d['internal_transfers']['net']))
    assert total == Decimal(d['net_change'])
    assert Decimal(d['opening_cash']) + Decimal(d['net_change']) == Decimal(d['closing_cash'])


def test_cash_flow_excludes_the_ipos_clearing_account(books):
    """1100011 holds the imported iPos sales history and nothing else. It sits
    in the cash band but is not spendable cash; letting it in would swamp the
    statement with billions of imported movement."""
    ipos = ChartOfAccountsFactory(account_number=1100011, name='Kas Penjualan iPos (histori)', account_type='asset')
    _post(D2, 'invoice', (ipos, 'debit', 9_000_000), (books['revenue'], 'credit', 9_000_000))

    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert 1100011 not in [a['account_number'] for a in d['accounts']]
    assert Decimal(d['net_change']) == Decimal('1300000')


def test_cash_flow_xlsx_export(books):
    req = APIRequestFactory().get('/x/', {'date_from': FROM, 'date_to': TO, 'export': 'xlsx'})
    force_authenticate(req, user=AppUserFactory(pin='654321'))
    resp = CashFlowView.as_view()(req)
    assert resp.status_code == 200
    assert 'spreadsheetml' in resp['Content-Type']


def test_general_ledger_running_balance(books):
    d = _call(GeneralLedgerView, account='all', date_from=FROM, date_to=TO).data
    cash = next(a for a in d['accounts'] if a['account_number'] == 1100001)
    assert Decimal(cash['opening_balance']) == Decimal('0')
    assert Decimal(cash['closing_balance']) == Decimal('1300000')
    assert Decimal(cash['lines'][-1]['balance']) == Decimal('1300000')


def test_xlsx_export(books):
    # `export=xlsx` (not `format`, which DRF reserves) streams a workbook.
    req = APIRequestFactory().get('/x/', {'as_of': TO, 'export': 'xlsx'})
    force_authenticate(req, user=AppUserFactory(pin='654321'))
    resp = TrialBalanceView.as_view()(req)
    assert resp.status_code == 200
    assert 'spreadsheetml' in resp['Content-Type']
    assert len(resp.content) > 500


def test_invalid_date_returns_400(books):
    resp = _call(ProfitLossView, date_from='not-a-date', date_to=TO)
    assert resp.status_code == 400


def test_report_rejects_range_with_unposted_day(books):
    """A day inside the requested range with no JournalDayLog row (or
    is_posted=False) must 400 with the list of offending dates, instead of
    silently reporting a partially-posted period as complete."""
    D3 = datetime.date(2026, 1, 25)
    _le(books['cash'], 'debit', 50_000, D3, 'invoice')
    # The `books` fixture already swept the whole FROM..TO range, including
    # D3 — flip it back to "never swept" to simulate a day that was missed.
    JournalDayLog.objects.filter(date=D3).delete()

    resp = _call(ProfitLossView, date_from=FROM, date_to=TO)
    assert resp.status_code == 400
    assert D3.isoformat() in resp.data['unposted_dates']

    resp = _call(GeneralLedgerView, account='all', date_from=FROM, date_to=TO)
    assert resp.status_code == 400
    assert D3.isoformat() in resp.data['unposted_dates']

    resp = _call(CashFlowView, date_from=FROM, date_to=TO)
    assert resp.status_code == 400

    # as_of-style reports (Trial Balance / Balance Sheet) are cumulative from
    # the earliest ledger date, so an unposted day anywhere before as_of also
    # blocks them.
    resp = _call(TrialBalanceView, as_of=TO)
    assert resp.status_code == 400
    assert D3.isoformat() in resp.data['unposted_dates']

    resp = _call(BalanceSheetView, as_of=TO)
    assert resp.status_code == 400


def test_report_succeeds_once_the_extra_day_is_posted(books):
    # `books` already sweeps the whole FROM..TO range, so a transaction
    # landing on any day in it (D3 included) is fine out of the box — this
    # pins that a posted day with activity does NOT block the report.
    D3 = datetime.date(2026, 1, 25)
    _le(books['cash'], 'debit', 50_000, D3, 'invoice')

    resp = _call(ProfitLossView, date_from=FROM, date_to=TO)
    assert resp.status_code == 200
