"""Integration tests for the POS endpoint: POST /api/invoices/create/.

Exercises the full atomic chain — Invoice + InvoiceItems + double-entry
LedgerEntry rows + FIFO stock deduction — against a real test database.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import Invoice, InvoiceItem, LedgerEntry, InventoryBatch


def _payload(stock, gl_accounts, *, quantity, price, grand_total):
    return {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash"].id,
        "discount": 0,
        "tax": 0,
        "additional_charges": 0,
        "grand_total": grand_total,
        "items": [
            {"item_id": stock["item"].id, "price": price, "quantity": quantity},
        ],
    }


@pytest.mark.django_db
class TestInvoiceCreate:
    def test_sale_decrements_stock_and_writes_balanced_ledger(self, auth_api, stock, gl_accounts):
        payload = _payload(stock, gl_accounts, quantity=2, price=10000, grand_total=20000)

        res = auth_api.post(reverse("invoice-create"), payload, format="json")
        assert res.status_code in (200, 201), res.content

        # Stock deducted FIFO: 100 - 2 = 98
        batch = InventoryBatch.objects.get(pk=stock["batch"].pk)
        assert batch.quantity_remaining == Decimal("98.0000")

        inv = Invoice.objects.latest("id")
        assert InvoiceItem.objects.filter(invoice=inv).count() == 1

        # Double-entry must balance for this invoice.
        debits = sum(
            e.amount for e in LedgerEntry.objects.filter(invoice=inv, entry_type="debit")
        )
        credits = sum(
            e.amount for e in LedgerEntry.objects.filter(invoice=inv, entry_type="credit")
        )
        assert debits == credits
        # debit cash 20000 + debit COGS 10000 ; credit revenue 20000 + credit inventory 10000
        assert debits == Decimal("30000")

    def test_cash_account_balance_increases_by_grand_total(self, auth_api, stock, gl_accounts):
        cash = gl_accounts["cash"]
        before = cash.balance

        payload = _payload(stock, gl_accounts, quantity=1, price=10000, grand_total=10000)
        res = auth_api.post(reverse("invoice-create"), payload, format="json")
        assert res.status_code in (200, 201), res.content

        cash.refresh_from_db()
        assert cash.balance == before + Decimal("10000")

    def test_unknown_item_rolls_back_everything(self, auth_api, stock, gl_accounts):
        payload = _payload(stock, gl_accounts, quantity=1, price=10000, grand_total=10000)
        payload["items"][0]["item_id"] = 999999  # does not exist

        res = auth_api.post(reverse("invoice-create"), payload, format="json")

        assert res.status_code == 400
        # @transaction.atomic must roll the whole request back.
        assert Invoice.objects.count() == 0
        assert LedgerEntry.objects.count() == 0
        # Untouched stock.
        batch = InventoryBatch.objects.get(pk=stock["batch"].pk)
        assert batch.quantity_remaining == Decimal("100.0000")

    def test_empty_items_is_rejected(self, auth_api, stock, gl_accounts):
        payload = _payload(stock, gl_accounts, quantity=1, price=10000, grand_total=10000)
        payload["items"] = []

        res = auth_api.post(reverse("invoice-create"), payload, format="json")
        assert res.status_code == 400
        assert Invoice.objects.count() == 0

    def test_oversell_does_not_drive_stock_negative(self, auth_api, stock, gl_accounts):
        """EDGE CASE: selling more than is in stock.

        Documents current behaviour: the sale is accepted and the batch floors
        at 0 (never negative). If the business rule should instead BLOCK the
        sale, this test pins the gap so the change is deliberate.
        """
        payload = _payload(stock, gl_accounts, quantity=150, price=10000, grand_total=1500000)

        res = auth_api.post(reverse("invoice-create"), payload, format="json")
        assert res.status_code in (200, 201), res.content

        batch = InventoryBatch.objects.get(pk=stock["batch"].pk)
        assert batch.quantity_remaining >= Decimal("0")
        assert batch.quantity_remaining == Decimal("0.0000")
