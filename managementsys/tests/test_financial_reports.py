"""Financial statement endpoint tests (Laba Rugi, Neraca, Neraca Saldo,
Buku Besar, Arus Kas).

A small balanced set of LedgerEntry rows is posted, then every report is
exercised through its APIView and the derived totals are asserted.
"""
import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from managementsys.models import LedgerEntry
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
