"""Every invoice posting must be a balanced double entry.

The old posting code debited the payment account for the full ``grand_total`` but
credited revenue only for lines carrying an ``InventoryItem`` FK, and posted no leg
at all for ``tax``, ``additional_charges`` or discounts. That silently pushed the
whole ledger out of balance by the value of every treatment billed by name.

These tests pin the invariant: for any invoice, total debits equal total credits.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import ChartOfAccounts, Invoice, LedgerEntry


def _sides(invoice):
    entries = LedgerEntry.objects.filter(invoice=invoice)
    debit = sum((e.amount for e in entries if e.entry_type == "debit"), Decimal("0"))
    credit = sum((e.amount for e in entries if e.entry_type == "credit"), Decimal("0"))
    return debit, credit


def _post(auth_api, stock, gl_accounts, items, **totals):
    payload = {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash"].id,
        "discount": totals.get("discount", 0),
        "tax": totals.get("tax", 0),
        "additional_charges": totals.get("additional_charges", 0),
        "grand_total": totals["grand_total"],
        "items": items,
    }
    res = auth_api.post(reverse("invoice-create"), payload, format="json")
    assert res.status_code in (200, 201), res.content
    return Invoice.objects.latest("id")


@pytest.mark.django_db
class TestInvoicePostingBalances:
    def test_plain_sale_balances(self, auth_api, stock, gl_accounts):
        invoice = _post(
            auth_api, stock, gl_accounts,
            [{"item_id": stock["item"].id, "price": 10000, "quantity": 2}],
            grand_total=20000,
        )
        debit, credit = _sides(invoice)
        assert debit == credit

    def test_line_without_item_fk_still_credits_revenue(self, auth_api, stock, gl_accounts):
        """A treatment billed by name only — the case that broke the ledger."""
        invoice = _post(
            auth_api, stock, gl_accounts,
            [{"item_name": "Light Peel", "price": 300000, "quantity": 1}],
            grand_total=300000,
        )
        debit, credit = _sides(invoice)
        assert debit == credit
        assert credit == Decimal("300000")

    def test_mixed_linked_and_unlinked_lines_balance(self, auth_api, stock, gl_accounts):
        invoice = _post(
            auth_api, stock, gl_accounts,
            [
                {"item_id": stock["item"].id, "price": 10000, "quantity": 1},
                {"item_name": "Light Peel", "price": 300000, "quantity": 1},
            ],
            grand_total=310000,
        )
        debit, credit = _sides(invoice)
        assert debit == credit

    def test_tax_and_surcharge_get_their_own_legs(self, auth_api, stock, gl_accounts):
        invoice = _post(
            auth_api, stock, gl_accounts,
            [{"item_id": stock["item"].id, "price": 10000, "quantity": 1}],
            tax=1000, additional_charges=500, grand_total=11500,
        )
        debit, credit = _sides(invoice)
        assert debit == credit
        entries = LedgerEntry.objects.filter(invoice=invoice)
        assert entries.filter(account=gl_accounts["tax_payable"],
                              entry_type="credit", amount=Decimal("1000")).exists()
        assert entries.filter(account=gl_accounts["additional_charges"],
                              entry_type="credit", amount=Decimal("500")).exists()

    def test_discount_lands_in_contra_revenue(self, auth_api, stock, gl_accounts):
        invoice = _post(
            auth_api, stock, gl_accounts,
            [{"item_id": stock["item"].id, "price": 10000, "quantity": 1}],
            discount=2000, grand_total=8000,
        )
        debit, credit = _sides(invoice)
        assert debit == credit
        assert LedgerEntry.objects.filter(
            invoice=invoice, account=gl_accounts["sales_discount"],
            entry_type="debit", amount=Decimal("2000"),
        ).exists()

    def test_undeclared_discount_still_balances(self, auth_api, stock, gl_accounts):
        """grand_total below the line total with ``discount`` left at 0.

        Promotions and package redemptions reach the invoice this way, so the
        posting has to absorb the gap rather than go out of balance.
        """
        invoice = _post(
            auth_api, stock, gl_accounts,
            [{"item_id": stock["item"].id, "price": 10000, "quantity": 1}],
            grand_total=7500,
        )
        debit, credit = _sides(invoice)
        assert debit == credit
        assert LedgerEntry.objects.filter(
            invoice=invoice, account=gl_accounts["sales_discount"],
            entry_type="debit", amount=Decimal("2500"),
        ).exists()

    def test_edit_then_void_leaves_no_net_effect(self, auth_api, stock, gl_accounts):
        invoice = _post(
            auth_api, stock, gl_accounts,
            [{"item_name": "Light Peel", "price": 300000, "quantity": 1}],
            tax=1000, grand_total=301000,
        )
        detail = reverse("invoice-detail", args=[invoice.id])

        res = auth_api.put(detail, {
            "tax": 2000,
            "grand_total": 402000,
            "items": [{"item_name": "Dermapen", "price": 400000, "quantity": 1}],
        }, format="json")
        assert res.status_code == 200, res.content
        debit, credit = _sides(invoice)
        assert debit == credit

        assert auth_api.delete(detail).status_code in (200, 204)
        debit, credit = _sides(invoice)
        assert debit == credit
        for account in ChartOfAccounts.objects.all():
            assert account.balance == Decimal("0"), account


@pytest.mark.django_db
def test_ledger_balances_globally(auth_api, stock, gl_accounts):
    _post(auth_api, stock, gl_accounts,
          [{"item_id": stock["item"].id, "price": 10000, "quantity": 1},
           {"item_name": "Light Peel", "price": 300000, "quantity": 1}],
          discount=5000, tax=1000, additional_charges=500, grand_total=306500)

    entries = LedgerEntry.objects.all()
    debit = sum((e.amount for e in entries if e.entry_type == "debit"), Decimal("0"))
    credit = sum((e.amount for e in entries if e.entry_type == "credit"), Decimal("0"))
    assert debit == credit
