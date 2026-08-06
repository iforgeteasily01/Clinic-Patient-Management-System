"""Financial statement endpoint tests (Laba Rugi, Neraca, Neraca Saldo,
Buku Besar, Arus Kas).

A small balanced set of LedgerEntry rows is posted, then every report is
exercised through its APIView and the derived totals are asserted.
"""
import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from managementsys.models import JournalDayLog, LedgerEntry
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


def _le(account, entry_type, amount, when, source='manual'):
    LedgerEntry.objects.create(
        account=account, date=when, description='test',
        entry_type=entry_type, amount=Decimal(amount), source_type=source,
    )


@pytest.fixture
def books(db):
    cash = ChartOfAccountsFactory(account_number=1100000, name='Cash on Hand', account_type='asset')
    revenue = ChartOfAccountsFactory(account_number=4100000, name='Treatment Revenue', account_type='revenue')
    expense = ChartOfAccountsFactory(account_number=6100000, name='Salaries', account_type='expense')
    equity = ChartOfAccountsFactory(account_number=3100000, name="Owner's Capital", account_type='equity')

    _le(cash, 'debit', 1_000_000, D1, 'adjustment')     # capital in
    _le(equity, 'credit', 1_000_000, D1, 'adjustment')
    _le(cash, 'debit', 500_000, D2, 'invoice')          # sale
    _le(revenue, 'credit', 500_000, D2, 'invoice')
    _le(expense, 'debit', 200_000, D2, 'adjustment')    # expense paid cash
    _le(cash, 'credit', 200_000, D2, 'adjustment')

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


def test_cash_flow(books):
    d = _call(CashFlowView, date_from=FROM, date_to=TO).data
    assert Decimal(d['opening_cash']) == Decimal('0')
    assert Decimal(d['closing_cash']) == Decimal('1300000')
    assert Decimal(d['net_change']) == Decimal('1300000')


def test_general_ledger_running_balance(books):
    d = _call(GeneralLedgerView, account='all', date_from=FROM, date_to=TO).data
    cash = next(a for a in d['accounts'] if a['account_number'] == 1100000)
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
