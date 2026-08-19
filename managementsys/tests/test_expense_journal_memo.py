"""Expense-as-journal-entry: cash account, per-leg memos, memo inheritance.

Companion to ``test_expense_accounting.py`` (which covers the deferred-posting
lifecycle). This module covers the redesign that turns an Expense from "a bill
with lines" into one explicit journal entry:

  * one credit leg out of a *named* cash/bank ChartOfAccounts row
    (``Expense.payment_account``), or the AP control account when the expense
    is not fully paid at posting time
  * N debit legs, each carrying its own memo (``ExpenseItem.description``),
    with a blank line memo inheriting ``Expense.payment_memo``
  * the curated cash/bank account set — an asset account that is not behind an
    active PaymentMethod (inventory, AR, fixed assets) is not selectable
"""
import datetime
import importlib
from decimal import Decimal

import pytest
from django.apps import apps as global_apps
from django.urls import reverse
from django.utils import timezone

from managementsys.models import ChartOfAccounts, Expense, ExpenseItem, LedgerEntry
from managementsys.services.cash_accounts import cash_bank_account_ids
from managementsys.services.journal_engine import expense_leg_memo

from .factories import ChartOfAccountsFactory, PaymentMethodFactory


# ── Helpers ─────────────────────────────────────────────────────────────────

def _today():
    return timezone.localdate()


def _run_journal(auth_api, through=None):
    date_to = (through or _today()).isoformat()
    res = auth_api.post(reverse('accounting-journal-run'), {'date_to': date_to}, format='json')
    assert res.status_code == 200, res.content
    return res.json()


@pytest.fixture
def expense_account(db):
    return ChartOfAccountsFactory(
        account_number=6800000, name='Beban Listrik', account_type='expense',
    )


@pytest.fixture
def water_account(db):
    return ChartOfAccountsFactory(
        account_number=6800100, name='Beban Air', account_type='expense',
    )


def _create_expense(auth_api, **payload):
    body = {
        'expense_date': '2026-07-30',
        'amount_paid': 0,
        'items': [],
    }
    body.update(payload)
    return auth_api.post(reverse('accounting-expenses'), body, format='json')


# ── expense_leg_memo: the fallback chain ─────────────────────────────────────

@pytest.mark.django_db
class TestExpenseLegMemoResolution:
    """Line memo set / blank / both blank, on both sides of the entry."""

    def _expense(self, expense_account, *, payment_memo='', description='',
                 amount_paid=0, total=15000):
        expense = Expense.objects.create(
            expense_date=datetime.date(2026, 7, 30),
            payment_memo=payment_memo,
            total_amount=Decimal(total),
            amount_paid=Decimal(amount_paid),
        )
        item = ExpenseItem.objects.create(
            expense=expense, account=expense_account,
            description=description, amount=Decimal(total),
        )
        return expense, item

    def test_line_memo_wins_over_cash_side_memo(self, expense_account):
        expense, item = self._expense(
            expense_account, payment_memo='Pembayaran utilitas Juli 2026',
            description='Tagihan PLN Juli',
        )
        assert expense_leg_memo(expense, item) == 'Tagihan PLN Juli'

    def test_blank_line_memo_inherits_the_cash_side_memo(self, expense_account):
        expense, item = self._expense(
            expense_account, payment_memo='Pembayaran utilitas Juli 2026', description='',
        )
        assert expense_leg_memo(expense, item) == 'Pembayaran utilitas Juli 2026'

    def test_whitespace_only_line_memo_counts_as_blank(self, expense_account):
        expense, item = self._expense(
            expense_account, payment_memo='Pembayaran utilitas Juli 2026', description='   ',
        )
        assert expense_leg_memo(expense, item) == 'Pembayaran utilitas Juli 2026'

    def test_both_blank_falls_back_to_the_generated_text(self, expense_account):
        expense, item = self._expense(expense_account, payment_memo='', description='')
        assert expense_leg_memo(expense, item) == 'Beban: Beban Listrik'

    def test_credit_leg_uses_payment_memo_when_set(self, expense_account):
        expense, _ = self._expense(
            expense_account, payment_memo='Pembayaran utilitas Juli 2026', amount_paid=15000,
        )
        assert expense_leg_memo(expense) == 'Pembayaran utilitas Juli 2026'

    def test_credit_leg_fallback_is_payment_wording_when_paid(self, expense_account):
        expense, _ = self._expense(expense_account, amount_paid=15000)
        assert expense_leg_memo(expense) == f'Pembayaran beban #{expense.pk}'

    def test_credit_leg_fallback_is_payable_wording_when_unpaid(self, expense_account):
        expense, _ = self._expense(expense_account, amount_paid=0)
        assert expense_leg_memo(expense) == f'Utang beban #{expense.pk}'


# ── Curated cash/bank account set ────────────────────────────────────────────

@pytest.mark.django_db
class TestCashBankAccountSet:
    def test_set_is_the_cash_band_not_every_asset(self, gl_accounts):
        ids = cash_bank_account_ids()
        assert gl_accounts['cash'].id in ids
        assert gl_accounts['bank'].id in ids
        # Assets that are not cash locations must stay out.
        assert gl_accounts['inventory_asset'].id not in ids
        # …as must the iPos clearing account, retired by migration 0100 even
        # though it sits in the band.
        assert gl_accounts['legacy_clearing'].id not in ids

    def test_band_membership_does_not_depend_on_a_payment_method(self):
        """An e-wallet account with no PaymentMethod pointing at it is still a
        place money sits — the 11xxxxx band is the definition, not the set of
        accounts some active payment method happens to reference."""
        wallet = ChartOfAccountsFactory(
            account_number=1102500, name='Gopay 1', account_type='asset',
        )
        assert wallet.id in cash_bank_account_ids()

        method = PaymentMethodFactory(name='Gopay 1', linked_account=wallet)
        method.is_active = False
        method.save(update_fields=['is_active'])
        assert wallet.id in cash_bank_account_ids()

    def test_retired_account_is_excluded_unless_already_used(self):
        """Migration 0089 retires drained accounts with a name suffix (COA has
        no is_active flag). Those must not be offered — but one already used to
        pay an expense stays selectable so historical rows never fail
        validation on edit."""
        retired_account = ChartOfAccountsFactory(
            account_number=1102001, name='Debit BCA (nonaktif)', account_type='asset',
        )
        assert retired_account.id not in cash_bank_account_ids()

        Expense.objects.create(
            expense_date=datetime.date(2026, 7, 30),
            payment_account=retired_account, total_amount=Decimal('0'),
        )
        assert retired_account.id in cash_bank_account_ids()

    def test_endpoint_returns_only_cash_accounts_sorted_by_name(self, auth_api, gl_accounts):
        res = auth_api.get(reverse('accounting-cash-accounts'))
        assert res.status_code == 200, res.content
        rows = res.json()
        names = [r['name'] for r in rows]
        assert names == sorted(names)
        assert {'id', 'name', 'account_number', 'balance'} == set(rows[0])
        assert gl_accounts['inventory_asset'].id not in {r['id'] for r in rows}
        assert gl_accounts['cash'].id in {r['id'] for r in rows}


# ── Create / validate ────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestExpenseCashAccountValidation:
    def test_cash_account_outside_the_curated_set_is_rejected(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api,
            cash_account=gl_accounts['inventory_asset'].id,
            items=[{'account': expense_account.id, 'description': 'Listrik', 'amount': 15000}],
        )
        assert res.status_code == 400, res.content
        assert res.json()['error'] == 'Rekening yang dipilih bukan rekening kas/bank.'
        assert not Expense.objects.exists()

    def test_unknown_cash_account_is_rejected(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api,
            cash_account=999999,
            items=[{'account': expense_account.id, 'amount': 15000}],
        )
        assert res.status_code == 400, res.content
        assert res.json()['error'] == 'Rekening yang dipilih bukan rekening kas/bank.'

    def test_edit_rejects_a_cash_account_outside_the_curated_set(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api,
            cash_account=gl_accounts['cash'].id,
            items=[{'account': expense_account.id, 'amount': 15000}],
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')

        res = auth_api.put(
            reverse('accounting-expense-detail', args=[expense.id]),
            {
                'expense_date': '2026-07-30',
                'cash_account': gl_accounts['inventory_asset'].id,
                'items': [{'account': expense_account.id, 'amount': 15000}],
            },
            format='json',
        )
        assert res.status_code == 400, res.content
        expense.refresh_from_db()
        assert expense.payment_account_id == gl_accounts['cash'].id

    def test_legacy_payment_account_key_resolves_its_linked_account(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api,
            payment_account=gl_accounts['cash_method'].id,
            items=[{'account': expense_account.id, 'amount': 15000}],
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')
        assert expense.payment_method_id == gl_accounts['cash_method'].id
        assert expense.payment_account_id == gl_accounts['cash'].id

    def test_list_filters_by_cash_account(self, auth_api, gl_accounts, expense_account):
        _create_expense(
            auth_api, payment_memo='Dengan kas', cash_account=gl_accounts['cash'].id,
            items=[{'account': expense_account.id, 'amount': 15000}],
        )
        _create_expense(
            auth_api, payment_memo='Tanpa kas',
            items=[{'account': expense_account.id, 'amount': 15000}],
        )
        res = auth_api.get(reverse('accounting-expenses'), {'cash_account': gl_accounts['cash'].id})
        assert res.status_code == 200, res.content
        memos = [row['payment_memo'] for row in res.json()]
        assert memos == ['Dengan kas']


# ── resolved_legs preview ────────────────────────────────────────────────────

@pytest.mark.django_db
class TestResolvedLegsPreview:
    def test_preview_marks_the_inherited_memo(self, auth_api, gl_accounts, expense_account, water_account):
        res = _create_expense(
            auth_api,
            cash_account=gl_accounts['cash'].id,
            payment_memo='Pembayaran utilitas Juli 2026',
            amount_paid=4250000,
            items=[
                {'account': expense_account.id, 'description': 'Tagihan PLN Juli', 'amount': 3000000},
                {'account': water_account.id, 'description': '', 'amount': 1250000},
            ],
        )
        assert res.status_code == 201, res.content
        legs = res.json()['resolved_legs']

        credit = legs[0]
        assert credit['side'] == 'credit'
        assert credit['account_id'] == gl_accounts['cash'].id
        assert credit['account_name'] == 'Cash Drawer'
        assert credit['memo'] == 'Pembayaran utilitas Juli 2026'
        assert credit['inherited'] is False

        debits = legs[1:]
        assert [d['side'] for d in debits] == ['debit', 'debit']
        assert debits[0]['memo'] == 'Tagihan PLN Juli'
        assert debits[0]['inherited'] is False
        assert debits[1]['memo'] == 'Pembayaran utilitas Juli 2026'
        assert debits[1]['inherited'] is True

    def test_serializer_exposes_renamed_and_new_fields(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api,
            cash_account=gl_accounts['cash'].id,
            payment_memo='Memo kas',
            payment_account=gl_accounts['cash_method'].id,
            items=[{'account': expense_account.id, 'amount': 15000}],
        )
        assert res.status_code == 201, res.content
        body = res.json()
        # payment_account_name used to mean "payment method name" — it is now
        # payment_method_name, and the cash_account_* fields are the real COA.
        assert 'payment_account_name' not in body
        assert body['payment_method_name'] == 'Cash'
        assert body['cash_account'] == gl_accounts['cash'].id
        assert body['cash_account_name'] == 'Cash Drawer'
        assert body['cash_account_number'] == 1101000
        assert body['payment_memo'] == 'Memo kas'


# ── Posting: which account the credit leg lands on ───────────────────────────

@pytest.mark.django_db
class TestExpenseCreditLegTarget:
    def test_credit_targets_payment_account_when_fully_paid(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api,
            cash_account=gl_accounts['cash'].id,
            payment_memo='Pembayaran utilitas Juli 2026',
            amount_paid=15000,
            items=[{'account': expense_account.id, 'description': 'Tagihan PLN Juli', 'amount': 15000}],
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')
        assert expense.status == 'paid'

        _run_journal(auth_api, datetime.date(2026, 7, 30))

        credit = LedgerEntry.objects.get(expense=expense, entry_type='credit')
        assert credit.account_id == gl_accounts['cash'].id
        assert credit.description == 'Pembayaran utilitas Juli 2026'

        debit = LedgerEntry.objects.get(expense=expense, entry_type='debit')
        assert debit.account_id == expense_account.id
        assert debit.description == 'Tagihan PLN Juli'

    def test_credit_targets_ap_control_when_unpaid_even_with_a_cash_account(
        self, auth_api, gl_accounts, expense_account,
    ):
        res = _create_expense(
            auth_api,
            cash_account=gl_accounts['cash'].id,
            payment_memo='Tagihan Juli',
            amount_paid=0,
            items=[{'account': expense_account.id, 'amount': 15000}],
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')

        _run_journal(auth_api, datetime.date(2026, 7, 30))

        credit = LedgerEntry.objects.get(expense=expense, entry_type='credit')
        ap_control = ChartOfAccounts.objects.get(account_number=2100000)
        assert credit.account_id == ap_control.id
        assert credit.description == 'Tagihan Juli'
        gl_accounts['cash'].refresh_from_db()
        assert gl_accounts['cash'].balance == Decimal('0')

    def test_legacy_row_without_payment_account_still_credits_the_linked_account(
        self, auth_api, gl_accounts, expense_account,
    ):
        """A row written before the picker existed: payment_method only."""
        expense = Expense.objects.create(
            expense_date=datetime.date(2026, 7, 30),
            payment_method=gl_accounts['cash_method'],
            total_amount=Decimal('15000'), amount_paid=Decimal('15000'),
            status='paid',
        )
        ExpenseItem.objects.create(expense=expense, account=expense_account, amount=Decimal('15000'))

        _run_journal(auth_api, datetime.date(2026, 7, 30))

        credit = LedgerEntry.objects.get(expense=expense, entry_type='credit')
        assert credit.account_id == gl_accounts['cash'].id
        assert credit.description == f'Pembayaran beban #{expense.pk}'

    def test_payment_credits_the_expenses_cash_account(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api,
            cash_account=gl_accounts['cash'].id,
            payment_memo='Cicilan listrik',
            items=[{'account': expense_account.id, 'amount': 10000}],
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')
        _run_journal(auth_api, datetime.date(2026, 7, 30))

        res = auth_api.post(
            reverse('accounting-expense-pay', args=[expense.id]),
            {'amount': 4000},
            format='json',
        )
        assert res.status_code == 200, res.content

        pay_credit = LedgerEntry.objects.get(
            expense=expense, entry_type='credit', account=gl_accounts['cash'],
        )
        assert pay_credit.amount == Decimal('4000')
        # Still a payment memo even though the expense is only partly paid.
        assert pay_credit.description == 'Cicilan listrik'


# ── Edit of an already-posted expense ────────────────────────────────────────

@pytest.mark.django_db
class TestPostedExpenseEditCarriesNewMemos:
    def test_edit_memo_rows_carry_the_new_memos(self, auth_api, gl_accounts, expense_account):
        res = _create_expense(
            auth_api, expense_date='2026-07-01',
            cash_account=gl_accounts['cash'].id,
            payment_memo='Memo lama',
            items=[{'account': expense_account.id, 'description': 'Baris lama', 'amount': 15000}],
        )
        assert res.status_code == 201, res.content
        expense = Expense.objects.latest('id')
        _run_journal(auth_api, datetime.date(2026, 7, 1))
        expense.refresh_from_db()
        assert expense.posting_status == 'posted'

        res = auth_api.put(
            reverse('accounting-expense-detail', args=[expense.id]),
            {
                'expense_date': '2026-07-01',
                'cash_account': gl_accounts['cash'].id,
                'payment_memo': 'Memo baru sisi kas',
                'items': [
                    {'account': expense_account.id, 'description': 'Baris baru', 'amount': 40000},
                    # blank memo -> inherits the new cash-side memo
                    {'account': expense_account.id, 'description': '', 'amount': 5000},
                ],
            },
            format='json',
        )
        assert res.status_code == 200, res.content

        memo_rows = LedgerEntry.objects.filter(expense=expense, source_type='edit_memo')
        today = _today()
        assert memo_rows.exists()
        assert all(r.date == today for r in memo_rows)

        descriptions = set(memo_rows.values_list('description', flat=True))
        assert 'Baris baru' in descriptions
        assert 'Memo baru sisi kas' in descriptions          # inherited debit leg + credit leg
        # the reversal rows quote the memos that were posted originally
        assert f'Koreksi edit beban #{expense.pk}: Baris lama' in descriptions

        expense.refresh_from_db()
        assert expense.payment_memo == 'Memo baru sisi kas'
        assert expense.total_amount == Decimal('45000')

        expense_account.refresh_from_db()
        assert expense_account.balance == Decimal('45000')   # net: only the new total


# ── Data migration 0092 ──────────────────────────────────────────────────────

@pytest.mark.django_db
class TestPaymentAccountBackfillMigration:
    """The migration runs against the historical model state; ``--nomigrations``
    means the suite never replays it, so drive its RunPython callables directly
    with the live app registry (they only ever call ``apps.get_model``)."""

    @staticmethod
    def _migration():
        return importlib.import_module(
            'managementsys.migrations.0092_backfill_expense_payment_account'
        )

    def test_backfill_copies_linked_account_and_reverses(self, gl_accounts, expense_account):
        legacy = Expense.objects.create(
            expense_date=datetime.date(2026, 7, 30),
            payment_method=gl_accounts['cash_method'],
            total_amount=Decimal('15000'),
        )
        already_set = Expense.objects.create(
            expense_date=datetime.date(2026, 7, 30),
            payment_method=gl_accounts['cash_method'],
            payment_account=gl_accounts['bank'],
            total_amount=Decimal('15000'),
        )
        no_method = Expense.objects.create(
            expense_date=datetime.date(2026, 7, 30),
            total_amount=Decimal('15000'),
        )

        migration = self._migration()
        migration.backfill_payment_account(global_apps, None)

        legacy.refresh_from_db()
        already_set.refresh_from_db()
        no_method.refresh_from_db()
        assert legacy.payment_account_id == gl_accounts['cash'].id
        assert already_set.payment_account_id == gl_accounts['bank'].id   # untouched
        assert no_method.payment_account_id is None

        migration.clear_backfilled_payment_account(global_apps, None)

        legacy.refresh_from_db()
        already_set.refresh_from_db()
        assert legacy.payment_account_id is None
        # pointed at a different account than its method's -> left alone
        assert already_set.payment_account_id == gl_accounts['bank'].id

    def test_backfill_touches_no_ledger_entry(self, gl_accounts, expense_account):
        expense = Expense.objects.create(
            expense_date=datetime.date(2026, 7, 30),
            payment_method=gl_accounts['cash_method'],
            total_amount=Decimal('15000'),
        )
        LedgerEntry.objects.create(
            account=expense_account, date=datetime.date(2026, 7, 30),
            description='Beban: Beban Listrik', entry_type='debit',
            amount=Decimal('15000'), source_type='expense', expense=expense,
        )
        before = list(
            LedgerEntry.objects.values_list('id', 'account_id', 'description', 'entry_type', 'amount')
        )

        self._migration().backfill_payment_account(global_apps, None)

        after = list(
            LedgerEntry.objects.values_list('id', 'account_id', 'description', 'entry_type', 'amount')
        )
        assert before == after
