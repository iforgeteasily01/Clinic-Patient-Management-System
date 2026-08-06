"""Purchase-invoice inventory valuation + editing after a sale.

Covers:
  * InventoryBatch.value is stored as the TOTAL batch value (per-unit =
    value / quantity_initial), matching every consumer.
  * Editing a purchase whose stock has already partly sold reverses and re-posts
    the journal, keeps it balanced, freezes the sold units at the cost the sale
    expensed, and posts the price difference to the variance account.
  * Repeated edits stay balanced and correct.
  * Shrinking a line below what has already sold is refused.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import (
    ChartOfAccounts, InventoryBatch, LedgerEntry, PurchaseInvoice, Supplier,
)
from managementsys.views.inventory_page import _fifo_deduct

from .factories import InventoryItemFactory, WarehouseFactory

VARIANCE_NUMBER = 5200000


@pytest.fixture
def supplier(db):
    return Supplier.objects.create(name='PT Contoh')


@pytest.fixture
def stock(db):
    """A fresh item + warehouse with NO pre-existing batch, so the only stock is
    what the purchase creates (and FIFO draws from it)."""
    return {'item': InventoryItemFactory(selling_price=10000), 'warehouse': WarehouseFactory()}


def _payload(supplier, cash, warehouse, item, *, qty, cost):
    return {
        'supplier': supplier.id,
        'payment_account': cash.id,
        'warehouse': warehouse.id,
        'purchase_date': '2026-07-30',
        'items': [{
            'line_type': 'stock', 'item': item.id, 'item_name': item.name,
            'quantity': qty, 'unit': 'pcs', 'unit_cost': cost,
            'warehouse': warehouse.id,
        }],
    }


def _bal(number):
    acct = ChartOfAccounts.objects.filter(account_number=number).first()
    return acct.balance if acct else Decimal('0')


def _assert_balanced():
    debit = sum((e.amount for e in LedgerEntry.objects.filter(entry_type='debit')), Decimal('0'))
    credit = sum((e.amount for e in LedgerEntry.objects.filter(entry_type='credit')), Decimal('0'))
    assert debit == credit, f'journal not balanced: {debit} != {credit}'


def _create(auth_api, supplier, stock, gl, *, qty, cost):
    res = auth_api.post(
        reverse('accounting-purchases'),
        _payload(supplier, gl['cash_method'], stock['warehouse'], stock['item'], qty=qty, cost=cost),
        format='json',
    )
    assert res.status_code == 201, res.content
    return PurchaseInvoice.objects.get(pk=res.json()['id'])


def _run_journal(auth_api, through='2026-07-31'):
    """Phase 2: purchase invoices are unposted at creation. The price-variance
    tests below need a *posted* invoice (its edit-memo path is what recomputes
    variance live), so they sweep it first, mirroring how the app is actually
    used."""
    res = auth_api.post(reverse('accounting-journal-run'), {'date_to': through}, format='json')
    assert res.status_code == 200, res.content
    return res.json()


def _edit(auth_api, invoice, supplier, stock, gl, *, qty, cost):
    res = auth_api.put(
        reverse('accounting-purchase-detail', args=[invoice.id]),
        _payload(supplier, gl['cash_method'], stock['warehouse'], stock['item'], qty=qty, cost=cost),
        format='json',
    )
    return res


# ── Batch value ──────────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_purchase_batch_stores_total_value(auth_api, supplier, stock, gl_accounts):
    invoice = _create(auth_api, supplier, stock, gl_accounts, qty=10, cost=1000)
    batch = InventoryBatch.objects.get(purchase_invoice=invoice)
    assert batch.value == Decimal('10000')                 # total, not per-unit

    shortfall, cogs = _fifo_deduct(stock['item'].id, stock['warehouse'].id, Decimal('10'))
    assert shortfall == 0
    assert cogs == Decimal('10000')                        # full COGS on resale


# ── Edit after a sale ─────────────────────────────────────────────────────────

@pytest.mark.django_db
def test_edit_after_partial_sale_posts_variance_and_balances(auth_api, supplier, stock, gl_accounts):
    invoice = _create(auth_api, supplier, stock, gl_accounts, qty=10, cost=1000)
    _run_journal(auth_api)  # posted, so the edit below takes the edit-memo path
    # 4 of the 10 units sell (draws the batch down; mirrors a POS sale FIFO).
    _fifo_deduct(stock['item'].id, stock['warehouse'].id, Decimal('4'))

    res = _edit(auth_api, invoice, supplier, stock, gl_accounts, qty=10, cost=1500)
    assert res.status_code == 200, res.content

    _assert_balanced()
    # 4 sold units: (1500 − 1000) × 4 = 2000 extra cost recognised as variance.
    assert _bal(VARIANCE_NUMBER) == Decimal('2000')

    invoice.refresh_from_db()
    assert invoice.total_amount == Decimal('15000')

    frozen = InventoryBatch.objects.get(purchase_invoice=invoice, quantity_remaining=0)
    assert frozen.quantity_initial == Decimal('4')
    assert frozen.value == Decimal('4000')                 # anchored at old cost
    onhand = InventoryBatch.objects.get(purchase_invoice=invoice, quantity_remaining__gt=0)
    assert onhand.quantity_remaining == Decimal('6')
    assert onhand.value == Decimal('9000')                 # 6 × new 1500


@pytest.mark.django_db
def test_repeated_edit_stays_balanced(auth_api, supplier, stock, gl_accounts):
    invoice = _create(auth_api, supplier, stock, gl_accounts, qty=10, cost=1000)
    _run_journal(auth_api)  # posted, so both edits below take the edit-memo path
    _fifo_deduct(stock['item'].id, stock['warehouse'].id, Decimal('4'))
    assert _edit(auth_api, invoice, supplier, stock, gl_accounts, qty=10, cost=1500).status_code == 200
    # Two more units sell from the on-hand batch, then edit again.
    _fifo_deduct(stock['item'].id, stock['warehouse'].id, Decimal('2'))
    assert _edit(auth_api, invoice, supplier, stock, gl_accounts, qty=10, cost=2000).status_code == 200

    _assert_balanced()
    # 6 units total sold, now valued at 2000: variance vs the anchors
    #   (4 @ 1000) + (2 @ 1500) = 7000 anchor; 6 × 2000 = 12000 → variance 5000.
    assert _bal(VARIANCE_NUMBER) == Decimal('5000')


@pytest.mark.django_db
def test_edit_without_sales_posts_no_variance(auth_api, supplier, stock, gl_accounts):
    invoice = _create(auth_api, supplier, stock, gl_accounts, qty=10, cost=1000)
    assert _edit(auth_api, invoice, supplier, stock, gl_accounts, qty=8, cost=1200).status_code == 200
    _assert_balanced()
    assert _bal(VARIANCE_NUMBER) == Decimal('0')
    assert not InventoryBatch.objects.filter(purchase_invoice=invoice, quantity_remaining=0).exists()


@pytest.mark.django_db
def test_cannot_shrink_below_sold(auth_api, supplier, stock, gl_accounts):
    invoice = _create(auth_api, supplier, stock, gl_accounts, qty=10, cost=1000)
    _fifo_deduct(stock['item'].id, stock['warehouse'].id, Decimal('6'))
    res = _edit(auth_api, invoice, supplier, stock, gl_accounts, qty=5, cost=1000)
    assert res.status_code == 400
    assert 'terjual' in res.json()['error']
