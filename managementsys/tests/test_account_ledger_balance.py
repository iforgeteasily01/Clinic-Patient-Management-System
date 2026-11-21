import datetime
from decimal import Decimal

import pytest
from rest_framework.test import APIRequestFactory, force_authenticate

from managementsys.models import LedgerEntry
from managementsys.views.admin_views import AccountLedgerView
from managementsys.views.financial_reports_utils import ledger_rows_with_balance
from .factories import AppUserFactory, ChartOfAccountsFactory

D1 = datetime.date(2026, 3, 5)
D2 = datetime.date(2026, 3, 10)
D3 = datetime.date(2026, 3, 20)


def _le(account, entry_type, amount, when):
    return LedgerEntry.objects.create(
        account=account, date=when, description='test',
        entry_type=entry_type, amount=Decimal(amount), source_type='manual',
    )


@pytest.fixture
def cash(db):
    acc = ChartOfAccountsFactory(account_number=1100500, name='Cash Drawer', account_type='asset')
    _le(acc, 'debit', 200_000, D3)
    _le(acc, 'credit', 300_000, D2)
    _le(acc, 'debit', 1_000_000, D1)
    return acc


@pytest.fixture
def payable(db):
    acc = ChartOfAccountsFactory(account_number=2100500, name='Accounts Payable', account_type='liability')
    _le(acc, 'debit', 200_000, D2)
    _le(acc, 'credit', 500_000, D1)
    return acc


def _call(pk, **params):
    req = APIRequestFactory().get('/x/', params)
    force_authenticate(req, user=AppUserFactory(pin='654321'))
    return AccountLedgerView.as_view()(req, pk=pk)


def _dec(entries):
    return [Decimal(e['balance']) for e in entries]


def test_model_default_ordering_is_descending():
    assert LedgerEntry._meta.ordering == ['-date', '-created_at']


def test_rows_are_ascending(cash):
    rows, *_ = ledger_rows_with_balance(cash)
    dates = [r.date for r in rows]
    assert dates == [D1, D2, D3]
    assert dates == sorted(dates)
    entries = _call(cash.pk).data['entries']
    assert [e['date'] for e in entries] == [d.isoformat() for d in (D1, D2, D3)]


def test_running_balance_debit_natural(cash):
    rows, opening, closing, total_debit, total_credit = ledger_rows_with_balance(cash)
    assert opening == Decimal('0')
    assert [r.running_balance for r in rows] == [
        Decimal('1000000'), Decimal('700000'), Decimal('900000'),
    ]
    assert closing == Decimal('900000')
    assert total_debit == Decimal('1200000')
    assert total_credit == Decimal('300000')


def test_running_balance_credit_natural(payable):
    rows, opening, closing, _, _ = ledger_rows_with_balance(payable)
    assert opening == Decimal('0')
    assert [r.running_balance for r in rows] == [Decimal('500000'), Decimal('300000')]
    assert closing == Decimal('300000')


def test_endpoint_exposes_balance_per_row(cash):
    d = _call(cash.pk).data
    assert _dec(d['entries']) == [Decimal('1000000'), Decimal('700000'), Decimal('900000')]
    assert d['opening_balance'] == '0'
    assert Decimal(d['closing_balance']) == Decimal('900000')
    cash.refresh_from_db()
    assert d['account'] | {
        'id': cash.id,
        'account_number': 1100500,
        'name': 'Cash Drawer',
        'account_type': 'asset',
        'balance': str(cash.balance),
    } == d['account']
    assert d['account']['linked_kind'] is None
    assert Decimal(d['total_debit']) == Decimal('1200000')
    assert Decimal(d['total_credit']) == Decimal('300000')


def test_opening_balance_zero_without_date_from(cash):
    _, opening, _, _, _ = ledger_rows_with_balance(cash)
    assert opening == Decimal('0')
    assert _call(cash.pk).data['opening_balance'] == '0'


def test_opening_balance_present_with_date_from(cash):
    rows, opening, closing, _, _ = ledger_rows_with_balance(cash, date_from=D2)
    assert opening == Decimal('1000000')
    assert [r.date for r in rows] == [D2, D3]
    assert [r.running_balance for r in rows] == [Decimal('700000'), Decimal('900000')]
    assert closing == Decimal('900000')


def test_opening_balance_is_natural_signed(payable):
    _, opening, closing, _, _ = ledger_rows_with_balance(payable, date_from=D2)
    assert opening == Decimal('500000')
    assert closing == Decimal('300000')


def test_endpoint_opening_balance_with_date_from(cash):
    d = _call(cash.pk, date_from=D2.isoformat()).data
    assert Decimal(d['opening_balance']) == Decimal('1000000')
    assert Decimal(d['closing_balance']) == Decimal('900000')
    assert _dec(d['entries']) == [Decimal('700000'), Decimal('900000')]


def test_entry_type_filter_keeps_true_balances(cash):
    rows, opening, closing, total_debit, total_credit = ledger_rows_with_balance(
        cash, entry_type='debit',
    )
    assert [r.date for r in rows] == [D1, D3]
    assert [r.running_balance for r in rows] == [Decimal('1000000'), Decimal('900000')]
    assert closing == Decimal('900000')
    assert opening == Decimal('0')
    assert total_debit == Decimal('1200000')
    assert total_credit == Decimal('0')


def test_endpoint_entry_type_filter(cash):
    d = _call(cash.pk, entry_type='debit').data
    assert _dec(d['entries']) == [Decimal('1000000'), Decimal('900000')]
    assert Decimal(d['total_debit']) == Decimal('1200000')
    assert d['total_credit'] == '0'
    assert Decimal(d['closing_balance']) == Decimal('900000')


def test_entry_type_filter_with_date_from(cash):
    rows, opening, closing, _, _ = ledger_rows_with_balance(
        cash, date_from=D2, entry_type='credit',
    )
    assert [r.date for r in rows] == [D2]
    assert [r.running_balance for r in rows] == [Decimal('700000')]
    assert opening == Decimal('1000000')
    assert closing == Decimal('900000')


def test_date_to_bounds_the_walk(cash):
    rows, _, closing, _, _ = ledger_rows_with_balance(cash, date_to=D2)
    assert [r.date for r in rows] == [D1, D2]
    assert closing == Decimal('700000')


def test_empty_window(cash):
    rows, opening, closing, total_debit, total_credit = ledger_rows_with_balance(
        cash, date_from=datetime.date(2026, 4, 1), date_to=datetime.date(2026, 4, 30),
    )
    assert rows == []
    assert opening == Decimal('900000')
    assert closing == Decimal('900000')
    assert total_debit == Decimal('0')
    assert total_credit == Decimal('0')
