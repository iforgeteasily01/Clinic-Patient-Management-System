"""Running-balance tests for the COA ledger (Feature B of
docs/DESIGN_expense_redesign_and_coa_print.md §3).

Covers ``ledger_rows_with_balance()`` and the ``AccountLedgerView`` endpoint
that consumes it: natural-sign walking on both debit- and credit-normal
accounts, the opening balance, balances surviving an ``entry_type`` display
filter, and — the trap this whole feature can silently fail on — that the walk
runs over an ASCENDING queryset even though ``LedgerEntry.Meta.ordering`` is
descending.
"""
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
    """Debit-natural account: +1.000.000, −300.000, +200.000 → 900.000.

    Rows are written newest-first on purpose so anything relying on insertion
    order (or on the model's descending Meta.ordering) computes the wrong walk.
    """
    acc = ChartOfAccountsFactory(account_number=1100500, name='Cash Drawer', account_type='asset')
    _le(acc, 'debit', 200_000, D3)
    _le(acc, 'credit', 300_000, D2)
    _le(acc, 'debit', 1_000_000, D1)
    return acc


@pytest.fixture
def payable(db):
    """Credit-natural account: +500.000 (credit), −200.000 (debit) → 300.000."""
    acc = ChartOfAccountsFactory(account_number=2100500, name='Accounts Payable', account_type='liability')
    _le(acc, 'debit', 200_000, D2)
    _le(acc, 'credit', 500_000, D1)
    return acc


def _call(pk, **params):
    req = APIRequestFactory().get('/x/', params)
    force_authenticate(req, user=AppUserFactory(pin='654321'))
    return AccountLedgerView.as_view()(req, pk=pk)


def _dec(entries):
    """Serialized balances as Decimals — the exact string exponent of a money
    field depends on the DB backend, the value does not."""
    return [Decimal(e['balance']) for e in entries]


# ── the ordering trap ────────────────────────────────────────────────────────

def test_model_default_ordering_is_descending():
    """Guard for the assumption the rest of this module rests on: the model
    orders newest-first, so ledger_rows_with_balance() *must* re-order."""
    assert LedgerEntry._meta.ordering == ['-date', '-created_at']


def test_rows_are_ascending(cash):
    rows, *_ = ledger_rows_with_balance(cash)
    dates = [r.date for r in rows]
    assert dates == [D1, D2, D3]
    assert dates == sorted(dates)
    # …and the endpoint hands them to the client in the same order.
    entries = _call(cash.pk).data['entries']
    assert [e['date'] for e in entries] == [d.isoformat() for d in (D1, D2, D3)]


# ── running balance ──────────────────────────────────────────────────────────

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
    """A credit on a liability *increases* it; a debit pays it down."""
    rows, opening, closing, _, _ = ledger_rows_with_balance(payable)
    assert opening == Decimal('0')
    assert [r.running_balance for r in rows] == [Decimal('500000'), Decimal('300000')]
    assert closing == Decimal('300000')


def test_endpoint_exposes_balance_per_row(cash):
    d = _call(cash.pk).data
    assert _dec(d['entries']) == [Decimal('1000000'), Decimal('700000'), Decimal('900000')]
    assert d['opening_balance'] == '0'
    assert Decimal(d['closing_balance']) == Decimal('900000')
    # the account block and the totals are the frontend's existing contract.
    # The block is the full ChartOfAccountsSerializer (it also carries the
    # linkage the COA list dropped), so assert on the keys the ledger page
    # reads rather than on the exact dict.
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


# ── opening balance ──────────────────────────────────────────────────────────

def test_opening_balance_zero_without_date_from(cash):
    _, opening, _, _, _ = ledger_rows_with_balance(cash)
    assert opening == Decimal('0')
    assert _call(cash.pk).data['opening_balance'] == '0'


def test_opening_balance_present_with_date_from(cash):
    rows, opening, closing, _, _ = ledger_rows_with_balance(cash, date_from=D2)
    assert opening == Decimal('1000000')          # the D1 debit, before the window
    assert [r.date for r in rows] == [D2, D3]
    assert [r.running_balance for r in rows] == [Decimal('700000'), Decimal('900000')]
    assert closing == Decimal('900000')


def test_opening_balance_is_natural_signed(payable):
    """Raw net for a liability is negative (credit-heavy); the opening balance
    must read positive, matching signed_balance()."""
    _, opening, closing, _, _ = ledger_rows_with_balance(payable, date_from=D2)
    assert opening == Decimal('500000')
    assert closing == Decimal('300000')


def test_endpoint_opening_balance_with_date_from(cash):
    d = _call(cash.pk, date_from=D2.isoformat()).data
    assert Decimal(d['opening_balance']) == Decimal('1000000')
    assert Decimal(d['closing_balance']) == Decimal('900000')
    assert _dec(d['entries']) == [Decimal('700000'), Decimal('900000')]


# ── entry_type is a display filter, not a balance filter ─────────────────────

def test_entry_type_filter_keeps_true_balances(cash):
    rows, opening, closing, total_debit, total_credit = ledger_rows_with_balance(
        cash, entry_type='debit',
    )
    assert [r.date for r in rows] == [D1, D3]
    # 900.000 — not 1.200.000: the hidden credit still moved the balance.
    assert [r.running_balance for r in rows] == [Decimal('1000000'), Decimal('900000')]
    assert closing == Decimal('900000')
    assert opening == Decimal('0')
    # totals cover the displayed rows only — unchanged from the old behaviour
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


# ── window edges ─────────────────────────────────────────────────────────────

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
