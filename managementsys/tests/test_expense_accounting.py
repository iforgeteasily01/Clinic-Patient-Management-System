"""Integration tests for the Phase 3 Expense/ExpenseItem accounting.

Mirrors managementsys/tests/test_purchase_accounting.py and the memo-path
coverage in managementsys/tests/test_journal_engine.py, but for the new
Expense model instead of PurchaseInvoice:

  create  -> posting deferred (posting_status='unposted', zero LedgerEntry
             rows) until a journal run sweeps expense_date, exactly like
             Invoice/PurchaseInvoice/AccountTransfer in Phase 2.
  run     -> Dr each ExpenseItem.account, Cr AP control account (unpaid) or
             the payment method's linked account (paid in full at creation).
  edit/void of a posted expense -> same-day memo pair (edit_memo/void_memo),
             leaving the original expense_date's rows and JournalDayLog alone.

Also covers that TreatmentCategory no longer exposes cogs_account/expense_account.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    ChartOfAccounts, Expense, JournalDayLog, LedgerEntry, TreatmentCategory,
)

from .factories import ChartOfAccountsFactory


# ── Helpers ─────────────────────────────────────────────────────────────────

def _today():
    """The app's "today" — Django's active timezone, not the OS clock.

    Memo entries are dated ``timezone.now().date()``; on a developer machine
    whose OS zone is ahead of ``settings.TIME_ZONE`` (UTC) ``date.today()``
    can already be tomorrow, so asserting against it flakes for hours a day.
    """
    return timezone.localdate()


def _run_journal(auth_api, through=None):
    date_to = (through or _today()).isoformat()
    res = auth_api.post(reverse('accounting-journal-run'), {'date_to': date_to}, format='json')
    assert res.status_code == 200, res.content
    return res.json()


def _bal(account_number):
    return ChartOfAccounts.objects.get(account_number=account_number).balance


def _ledger_totals():
    debit = sum((e.amount for e in LedgerEntry.objects.filter(entry_type='debit')), Decimal('0'))
    credit = sum((e.amount for e in LedgerEntry.objects.filter(entry_type='credit')), Decimal('0'))
    return debit, credit


def _assert_balanced():
    debit, credit = _ledger_totals()
    assert debit == credit, f'journal not balanced: debit {debit} != credit {credit}'


@pytest.fixture
def expense_account(db):
    return ChartOfAccountsFactory(
        account_number=6800000, name='Office Supplies', account_type='expense',
    )


def _expense_payload(*, date='2026-07-30', account_id, amount=15000, payment_account=None, amount_paid=0):
    payload = {
        'expense_date': date,
        'amount_paid': amount_paid,
        'items': [{'account': account_id, 'description': 'Listrik', 'amount': amount}],
    }
    if payment_account is not None:
        payload['payment_account'] = payment_account
    return payload


# ── Create ──────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestExpenseCreatePosting:
    def test_create_is_unposted_with_zero_ledger_effect(self, auth_api, gl_accounts, expense_account):
        res = auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=15000),
            format='json',
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')
        assert expense.posting_status == 'unposted'
        assert expense.total_amount == Decimal('15000')
        assert LedgerEntry.objects.filter(expense=expense).count() == 0
        expense_account.refresh_from_db()
        assert expense_account.balance == Decimal('0')

    def test_item_without_account_rejected(self, auth_api, gl_accounts):
        res = auth_api.post(
            reverse('accounting-expenses'),
            {'expense_date': '2026-07-30',
             'items': [{'description': 'no account', 'amount': 1000}]},
            format='json',
        )
        assert res.status_code == 400, res.content
        assert not Expense.objects.exists()


# ── Journal run posting ──────────────────────────────────────────────────────

@pytest.mark.django_db
class TestExpenseJournalRun:
    def test_run_posts_expense_to_ap_control_when_unpaid(self, auth_api, gl_accounts, expense_account):
        res = auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=15000),
            format='json',
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')

        _run_journal(auth_api, datetime.date(2026, 7, 30))

        expense.refresh_from_db()
        assert expense.posting_status == 'posted'
        assert _bal(6800000) == Decimal('15000')       # Dr expense account
        ap_control = ChartOfAccounts.objects.get(account_number=2100000)
        assert ap_control.balance == Decimal('15000')  # Cr AP control (generic clearing)
        _assert_balanced()

    def test_run_posts_expense_to_payment_method_when_paid_in_full(self, auth_api, gl_accounts, expense_account):
        res = auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(
                account_id=expense_account.id, amount=15000,
                payment_account=gl_accounts['cash_method'].id, amount_paid=15000,
            ),
            format='json',
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')
        assert expense.status == 'paid'

        _run_journal(auth_api, datetime.date(2026, 7, 30))

        expense.refresh_from_db()
        assert expense.posting_status == 'posted'
        assert _bal(6800000) == Decimal('15000')        # Dr expense account
        assert _bal(1101000) == Decimal('-15000')        # Cr cash (funds left)
        _assert_balanced()

    def test_run_sweeps_unposted_expense_alongside_other_documents(self, auth_api, gl_accounts, expense_account):
        auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=5000, date='2026-07-01'),
            format='json',
        )
        result = _run_journal(auth_api, datetime.date(2026, 7, 1))
        assert result['status'] == 'completed'
        assert result['documents_posted'] == 1

        day = JournalDayLog.objects.get(date=datetime.date(2026, 7, 1))
        assert day.is_posted is True


# ── Void & edit memo round-trips ─────────────────────────────────────────────

@pytest.mark.django_db
class TestExpenseVoidMemo:
    def test_void_of_posted_expense_writes_same_day_reversal_and_leaves_original_day_alone(
        self, auth_api, gl_accounts, expense_account,
    ):
        auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=15000, date='2026-07-01'),
            format='json',
        )
        expense = Expense.objects.latest('id')
        _run_journal(auth_api, datetime.date(2026, 7, 1))
        expense.refresh_from_db()
        assert expense.posting_status == 'posted'

        original_day = JournalDayLog.objects.get(date=datetime.date(2026, 7, 1))
        assert original_day.is_posted is True

        expense_id = expense.id
        res = auth_api.delete(reverse('accounting-expense-detail', args=[expense_id]))
        assert res.status_code == 204, res.content

        original_day.refresh_from_db()
        assert original_day.is_posted is True

        # Expense has no is_voided flag (unlike PurchaseInvoice) — DELETE
        # really removes the row after writing the reversal memo, which nulls
        # LedgerEntry.expense via SET_NULL. The memo/original rows themselves
        # (and their plain-text description) survive for audit purposes;
        # only the FK link to the now-gone Expense row is gone. Identify them
        # by source_type/date/account instead of the expense FK.
        assert not Expense.objects.filter(pk=expense_id).exists()

        today = _today()
        memo_entries = LedgerEntry.objects.filter(
            source_type='void_memo', date=today, account=expense_account,
        )
        assert memo_entries.exists()

        original_entries = LedgerEntry.objects.filter(
            source_type='expense', date=datetime.date(2026, 7, 1), account=expense_account,
        )
        assert original_entries.exists()

        expense_account.refresh_from_db()
        assert expense_account.balance == Decimal('0')   # memo fully offsets original
        _assert_balanced()

    def test_void_of_unposted_expense_writes_no_memo(self, auth_api, gl_accounts, expense_account):
        auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=15000),
            format='json',
        )
        expense = Expense.objects.latest('id')
        assert expense.posting_status == 'unposted'

        res = auth_api.delete(reverse('accounting-expense-detail', args=[expense.id]))
        assert res.status_code == 204, res.content
        assert LedgerEntry.objects.filter(expense=expense).count() == 0


@pytest.mark.django_db
class TestExpenseEditMemo:
    def test_edit_of_posted_expense_writes_same_day_reversal_and_repost_pair(
        self, auth_api, gl_accounts, expense_account,
    ):
        auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=15000, date='2026-07-01'),
            format='json',
        )
        expense = Expense.objects.latest('id')
        _run_journal(auth_api, datetime.date(2026, 7, 1))

        original_day = JournalDayLog.objects.get(date=datetime.date(2026, 7, 1))

        res = auth_api.put(
            reverse('accounting-expense-detail', args=[expense.id]),
            {
                'expense_date': '2026-07-01',
                'items': [{'account': expense_account.id, 'description': 'Listrik', 'amount': 40000}],
            },
            format='json',
        )
        assert res.status_code == 200, res.content

        original_day.refresh_from_db()
        assert original_day.is_posted is True

        memo_entries = LedgerEntry.objects.filter(expense=expense, source_type='edit_memo')
        assert memo_entries.exists()
        today = _today()
        assert all(e.date == today for e in memo_entries)

        expense_account.refresh_from_db()
        assert expense_account.balance == Decimal('40000')   # net effect: only the new total

        expense.refresh_from_db()
        assert expense.posting_status == 'posted'   # still posted, not reopened
        assert expense.total_amount == Decimal('40000')
        _assert_balanced()

    def test_edit_of_unposted_expense_writes_no_memo(self, auth_api, gl_accounts, expense_account):
        auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=15000),
            format='json',
        )
        expense = Expense.objects.latest('id')
        assert expense.posting_status == 'unposted'

        res = auth_api.put(
            reverse('accounting-expense-detail', args=[expense.id]),
            {
                'expense_date': str(expense.expense_date),
                'items': [{'account': expense_account.id, 'description': 'Listrik', 'amount': 40000}],
            },
            format='json',
        )
        assert res.status_code == 200, res.content
        assert LedgerEntry.objects.filter(expense=expense).count() == 0
        expense.refresh_from_db()
        assert expense.posting_status == 'unposted'
        assert expense.total_amount == Decimal('40000')


# ── Pay ─────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestExpensePayment:
    def test_partial_payment_draws_down_ap_and_cash(self, auth_api, gl_accounts, expense_account):
        auth_api.post(
            reverse('accounting-expenses'),
            _expense_payload(account_id=expense_account.id, amount=10000, date='2026-07-30'),
            format='json',
        )
        expense = Expense.objects.latest('id')
        _run_journal(auth_api, datetime.date(2026, 7, 30))  # AP must be posted before it can be paid down

        res = auth_api.post(
            reverse('accounting-expense-pay', args=[expense.id]),
            {'amount': 4000, 'payment_account': gl_accounts['cash_method'].id},
            format='json',
        )
        assert res.status_code == 200, res.content

        ap_control = ChartOfAccounts.objects.get(account_number=2100000)
        assert ap_control.balance == Decimal('6000')    # 10000 - 4000
        assert _bal(1101000) == Decimal('-4000')         # cash credited out
        expense.refresh_from_db()
        assert expense.status == 'partial'
        _assert_balanced()


# ── TreatmentCategory no longer exposes cogs/expense accounts ───────────────

@pytest.mark.django_db
class TestTreatmentCategoryHasNoCogsExpenseAccounts:
    def test_fields_are_gone(self, db):
        cat = TreatmentCategory.objects.create(name='Facial')
        assert cat.revenue_account_id is not None
        assert not hasattr(cat, 'cogs_account')
        assert not hasattr(cat, 'expense_account')
        field_names = {f.name for f in TreatmentCategory._meta.get_fields()}
        assert 'cogs_account' not in field_names
        assert 'expense_account' not in field_names
