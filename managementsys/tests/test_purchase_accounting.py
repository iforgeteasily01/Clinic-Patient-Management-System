"""Integration tests for purchase-invoice accrual accounting.

The purchasing flow now posts full double-entry:
  create  -> Dr Inventory/Expense , Cr Accounts Payable (per vendor)
  pay     -> Dr Accounts Payable  , Cr Cash/Bank
  edit    -> reverse the above and re-post the replaced lines
  void    -> reverse the creation postings

The invariants defended here:
  * every posting keeps the journal balanced (Σ debit == Σ credit)
  * a vendor's AP account tracks exactly what is owed
  * create -> void and create -> edit -> void return every balance to baseline
  * the outstanding-only backfill command establishes AP against Opening
    Balance Equity and is idempotent.
"""
from decimal import Decimal

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    ChartOfAccounts, LedgerEntry, PurchaseInvoice, Supplier,
)

from .factories import ChartOfAccountsFactory


# ── Helpers ─────────────────────────────────────────────────────────────────

def _bal(account_number):
    return ChartOfAccounts.objects.get(account_number=account_number).balance


def _ledger_totals():
    debit = sum((e.amount for e in LedgerEntry.objects.filter(entry_type='debit')), Decimal('0'))
    credit = sum((e.amount for e in LedgerEntry.objects.filter(entry_type='credit')), Decimal('0'))
    return debit, credit


def _assert_balanced():
    debit, credit = _ledger_totals()
    assert debit == credit, f'journal not balanced: debit {debit} != credit {credit}'


def _stock_payload(supplier, cash, warehouse, item, *, qty, cost, date='2026-07-30'):
    return {
        'supplier': supplier.id,
        'payment_account': cash.id,
        'warehouse': warehouse.id,
        'purchase_date': date,
        'items': [{
            'line_type': 'stock', 'item': item.id, 'item_name': item.name,
            'quantity': qty, 'unit': 'pcs', 'unit_cost': cost,
            'warehouse': warehouse.id,
        }],
    }


@pytest.fixture
def supplier(db):
    """A supplier — its AP account is auto-created by Supplier.save()."""
    return Supplier.objects.create(name='PT Contoh')


@pytest.fixture
def expense_account(db):
    return ChartOfAccountsFactory(
        account_number=6800000, name='Office Supplies', account_type='expense',
    )


# ── Create ──────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestPurchaseCreatePosting:
    def test_stock_purchase_posts_inventory_and_ap(self, auth_api, supplier, stock, gl_accounts):
        res = auth_api.post(
            reverse('accounting-purchases'),
            _stock_payload(supplier, gl_accounts['cash'], stock['warehouse'], stock['item'],
                           qty=10, cost=1000),
            format='json',
        )
        assert res.status_code == 201, res.content

        supplier.refresh_from_db()
        assert _bal(1300000) == Decimal('10000')          # Dr Inventory
        assert supplier.ap_account.balance == Decimal('10000')  # Cr AP-vendor
        # AP account nests under the AP control (2100000)
        assert supplier.ap_account.parent.account_number == 2100000
        _assert_balanced()

    def test_expense_purchase_posts_expense_and_ap(self, auth_api, supplier, stock, gl_accounts, expense_account):
        res = auth_api.post(
            reverse('accounting-purchases'),
            {
                'supplier': supplier.id,
                'payment_account': gl_accounts['cash'].id,
                'purchase_date': '2026-07-30',
                'items': [{
                    'line_type': 'expense', 'item_name': 'Consulting',
                    'quantity': 1, 'unit_cost': 25000,
                    'expense_account': expense_account.id,
                }],
            },
            format='json',
        )
        assert res.status_code == 201, res.content
        supplier.refresh_from_db()
        assert _bal(6800000) == Decimal('25000')
        assert supplier.ap_account.balance == Decimal('25000')
        _assert_balanced()

    def test_expense_line_without_account_rejected(self, auth_api, supplier, gl_accounts):
        res = auth_api.post(
            reverse('accounting-purchases'),
            {
                'supplier': supplier.id,
                'payment_account': gl_accounts['cash'].id,
                'purchase_date': '2026-07-30',
                'items': [{'line_type': 'expense', 'item_name': 'X',
                           'quantity': 1, 'unit_cost': 5000}],
            },
            format='json',
        )
        assert res.status_code == 400, res.content
        assert not PurchaseInvoice.objects.exists()

    def test_additional_cost_on_all_expense_invoice_stays_balanced(
        self, auth_api, supplier, gl_accounts, expense_account,
    ):
        """An additional cost with no stock unit to absorb it must still leave
        debits == the AP credit (residual reconciliation)."""
        res = auth_api.post(
            reverse('accounting-purchases'),
            {
                'supplier': supplier.id,
                'payment_account': gl_accounts['cash'].id,
                'purchase_date': '2026-07-30',
                'items': [{'line_type': 'expense', 'item_name': 'Service',
                           'quantity': 1, 'unit_cost': 20000,
                           'expense_account': expense_account.id}],
                'additional_costs': [{'name': 'Ongkir', 'modifier': 'add',
                                      'amount_type': 'cash', 'amount': 3000}],
            },
            format='json',
        )
        assert res.status_code == 201, res.content
        supplier.refresh_from_db()
        assert supplier.ap_account.balance == Decimal('23000')
        assert _bal(6800000) == Decimal('23000')  # residual folded into the expense debit
        _assert_balanced()


# ── Pay ─────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestPurchasePayment:
    def test_partial_payment_draws_down_ap_and_cash(self, auth_api, supplier, stock, gl_accounts):
        auth_api.post(
            reverse('accounting-purchases'),
            _stock_payload(supplier, gl_accounts['cash'], stock['warehouse'], stock['item'],
                           qty=10, cost=1000),
            format='json',
        )
        invoice = PurchaseInvoice.objects.latest('id')

        res = auth_api.post(reverse('accounting-purchase-pay', args=[invoice.id]),
                            {'amount': 4000}, format='json')
        assert res.status_code == 200, res.content

        supplier.refresh_from_db()
        assert supplier.ap_account.balance == Decimal('6000')   # 10000 - 4000
        assert _bal(1101000) == Decimal('-4000')                # cash credited out
        invoice.refresh_from_db()
        assert invoice.status == 'partial'
        _assert_balanced()


# ── Void & edit round-trips ─────────────────────────────────────────────────

@pytest.mark.django_db
class TestPurchaseRoundTrips:
    def test_create_then_void_returns_to_baseline(self, auth_api, supplier, stock, gl_accounts):
        auth_api.post(
            reverse('accounting-purchases'),
            _stock_payload(supplier, gl_accounts['cash'], stock['warehouse'], stock['item'],
                           qty=10, cost=1000),
            format='json',
        )
        invoice = PurchaseInvoice.objects.latest('id')

        res = auth_api.delete(reverse('accounting-purchase-detail', args=[invoice.id]))
        assert res.status_code == 200, res.content

        supplier.refresh_from_db()
        assert _bal(1300000) == Decimal('0')
        assert supplier.ap_account.balance == Decimal('0')
        _assert_balanced()

    def test_create_edit_void_is_value_neutral(self, auth_api, supplier, stock, gl_accounts):
        auth_api.post(
            reverse('accounting-purchases'),
            _stock_payload(supplier, gl_accounts['cash'], stock['warehouse'], stock['item'],
                           qty=10, cost=1000),
            format='json',
        )
        invoice = PurchaseInvoice.objects.latest('id')

        # Edit to a larger order.
        res = auth_api.put(
            reverse('accounting-purchase-detail', args=[invoice.id]),
            _stock_payload(supplier, gl_accounts['cash'], stock['warehouse'], stock['item'],
                           qty=20, cost=1000),
            format='json',
        )
        assert res.status_code == 200, res.content
        supplier.refresh_from_db()
        assert supplier.ap_account.balance == Decimal('20000')
        assert _bal(1300000) == Decimal('20000')
        _assert_balanced()

        # Void → everything back to baseline.
        res = auth_api.delete(reverse('accounting-purchase-detail', args=[invoice.id]))
        assert res.status_code == 200, res.content
        supplier.refresh_from_db()
        assert supplier.ap_account.balance == Decimal('0')
        assert _bal(1300000) == Decimal('0')
        _assert_balanced()


# ── Backfill command ────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestOutstandingBackfill:
    def _legacy_invoice(self, supplier, cash, *, total, paid=Decimal('0')):
        """A pre-existing invoice with no ledger postings (as before this change)."""
        status = 'unpaid' if paid == 0 else ('paid' if paid >= total else 'partial')
        return PurchaseInvoice.objects.create(
            supplier=supplier, payment_account=cash,
            purchase_date=timezone.now().date(),
            total_amount=total, amount_paid=paid, status=status,
        )

    def test_backfill_posts_outstanding_against_opening_balance_equity(
        self, db, supplier, gl_accounts,
    ):
        self._legacy_invoice(supplier, gl_accounts['cash'], total=Decimal('50000'))
        self._legacy_invoice(supplier, gl_accounts['cash'],
                             total=Decimal('30000'), paid=Decimal('10000'))   # partial → 20000
        self._legacy_invoice(supplier, gl_accounts['cash'],
                             total=Decimal('10000'), paid=Decimal('10000'))   # paid → skipped

        call_command('provision_supplier_ap_accounts', '--backfill')

        supplier.refresh_from_db()
        assert supplier.ap_account.balance == Decimal('70000')   # 50000 + 20000
        assert _bal(3900000) == Decimal('-70000')                # OBE debited
        _assert_balanced()

    def test_backfill_is_idempotent(self, db, supplier, gl_accounts):
        self._legacy_invoice(supplier, gl_accounts['cash'], total=Decimal('50000'))
        call_command('provision_supplier_ap_accounts', '--backfill')
        call_command('provision_supplier_ap_accounts', '--backfill')

        supplier.refresh_from_db()
        assert supplier.ap_account.balance == Decimal('50000')   # not doubled
        assert _bal(3900000) == Decimal('-50000')
        _assert_balanced()


# ── Vendor drill-down endpoint ──────────────────────────────────────────────

@pytest.mark.django_db
class TestSupplierAccountEndpoint:
    def test_account_view_aggregates_items_and_outstanding(self, auth_api, supplier, stock, gl_accounts):
        auth_api.post(
            reverse('accounting-purchases'),
            _stock_payload(supplier, gl_accounts['cash'], stock['warehouse'], stock['item'],
                           qty=10, cost=1000),
            format='json',
        )
        res = auth_api.get(reverse('accounting-supplier-account', args=[supplier.id]))
        assert res.status_code == 200, res.content
        body = res.json()
        assert body['outstanding'] == '10000.00'
        assert body['invoice_count'] == 1
        assert len(body['items']) == 1
        assert body['items'][0]['total_spend'] == '10000.00'
        assert body['supplier']['ap_account_number'] == supplier.ap_account.account_number
