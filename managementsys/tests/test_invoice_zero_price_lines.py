"""A Rp 0 line is a real line, and the backend has to keep it.

The POS prints free items, bonuses and package redemptions on the receipt, so the
invoice the server stores must carry the same lines — with the same prices — or
the receipt and the record disagree about what was sold. A zero price also must
not stop the stock and COGS side: the goods left the shelf whether or not the
patient was charged for them.

These tests pin what survives the round trip: the line itself, its price, its
per-line discount, and the stock movement behind it.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import (
    InventoryBatch, Invoice, InvoiceItem, LedgerEntry,
)


def _run_journal(auth_api, through=None):
    date_to = (through or datetime.date.today()).isoformat()
    res = auth_api.post(reverse("accounting-journal-run"), {"date_to": date_to}, format="json")
    assert res.status_code == 200, res.content
    return res.json()


def _sides(invoice):
    entries = LedgerEntry.objects.filter(invoice=invoice)
    debit = sum((e.amount for e in entries if e.entry_type == "debit"), Decimal("0"))
    credit = sum((e.amount for e in entries if e.entry_type == "credit"), Decimal("0"))
    return debit, credit


def _create(auth_api, stock, gl_accounts, items, grand_total):
    res = auth_api.post(reverse("invoice-create"), {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash_method"].id,
        "discount": 0, "tax": 0, "additional_charges": 0,
        "grand_total": grand_total,
        "items": items,
    }, format="json")
    assert res.status_code == 201, res.content
    return Invoice.objects.latest("id")


@pytest.mark.django_db
class TestZeroPriceLines:
    def test_zero_price_line_is_stored_not_dropped(self, auth_api, stock, gl_accounts):
        invoice = _create(auth_api, stock, gl_accounts, [
            {"item_id": stock["item"].id, "price": 10000, "quantity": 1},
            {"item_id": stock["item"].id, "price": 0, "quantity": 1},
        ], grand_total=10000)

        lines = list(invoice.items.order_by("id"))
        assert len(lines) == 2
        assert lines[1].price == Decimal("0")
        assert lines[1].quantity == Decimal("1")

    def test_free_line_still_leaves_the_shelf(self, auth_api, stock, gl_accounts):
        """A giveaway costs the clinic its COGS even though it earns no revenue."""
        invoice = _create(auth_api, stock, gl_accounts, [
            {"item_id": stock["item"].id, "price": 0, "quantity": 2},
        ], grand_total=0)
        _run_journal(auth_api)

        batch = InventoryBatch.objects.get(pk=stock["batch"].pk)
        assert batch.quantity_remaining == Decimal("98")

        cogs = LedgerEntry.objects.filter(
            invoice=invoice, account=gl_accounts["cogs"], entry_type="debit")
        assert cogs.exists()
        debit, credit = _sides(invoice)
        assert debit == credit

    def test_zero_price_line_earns_no_revenue(self, auth_api, stock, gl_accounts):
        invoice = _create(auth_api, stock, gl_accounts, [
            {"item_id": stock["item"].id, "price": 10000, "quantity": 1},
            {"item_id": stock["item"].id, "price": 0, "quantity": 1},
        ], grand_total=10000)
        _run_journal(auth_api)

        revenue = sum(
            (e.amount for e in LedgerEntry.objects.filter(
                invoice=invoice, account=gl_accounts["revenue"], entry_type="credit")),
            Decimal("0"),
        )
        assert revenue == Decimal("10000")
        debit, credit = _sides(invoice)
        assert debit == credit

    def test_line_named_but_not_linked_survives(self, auth_api, stock, gl_accounts):
        """The POS sends the item code as item_name when a line has no description."""
        invoice = _create(auth_api, stock, gl_accounts, [
            {"item_name": "BONUS-01", "price": 0, "quantity": 1},
            {"item_name": "Light Peel", "price": 300000, "quantity": 1},
        ], grand_total=300000)

        names = [l.item_name for l in invoice.items.order_by("id")]
        assert names == ["BONUS-01", "Light Peel"]

    def test_per_line_discount_is_stored_as_sent(self, auth_api, stock, gl_accounts):
        """price stays gross; discount_pct travels with the line or the total lies."""
        invoice = _create(auth_api, stock, gl_accounts, [
            {"item_id": stock["item"].id, "price": 10000, "quantity": 1,
             "discount_pct": 25},
        ], grand_total=7500)

        line = invoice.items.get()
        assert line.price == Decimal("10000")
        assert line.discount_pct == Decimal("25.00")


@pytest.mark.django_db
class TestZeroPriceLinesSurviveAnEdit:
    def test_edit_keeps_the_free_line_and_its_discount(self, auth_api, stock, gl_accounts):
        invoice = _create(auth_api, stock, gl_accounts, [
            {"item_id": stock["item"].id, "price": 10000, "quantity": 1,
             "discount_pct": 25},
            {"item_id": stock["item"].id, "price": 0, "quantity": 1},
        ], grand_total=7500)
        _run_journal(auth_api)

        # The POS resends every line it printed, prices and discounts included.
        res = auth_api.patch(
            reverse("invoice-detail", args=[invoice.id]),
            {"grand_total": 9000, "items": [
                {"item_id": stock["item"].id, "price": 12000, "quantity": 1,
                 "discount_pct": 25},
                {"item_id": stock["item"].id, "price": 0, "quantity": 1},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content

        lines = list(invoice.items.order_by("id"))
        assert [l.price for l in lines] == [Decimal("12000"), Decimal("0")]
        assert lines[0].discount_pct == Decimal("25.00")
        debit, credit = _sides(invoice)
        assert debit == credit
