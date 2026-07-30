"""Guards on which invoices the ledger is expected to cover.

``invoices_missing_from_ledger`` decides what ``repair_accounting_balance`` posts and
what ``check_accounting_health`` fails on. Two exclusions in it are deliberate policy
rather than convenience, and both are easy to undo by accident:

* iPos-imported rows stay out even when dated after go-live — iPos ran alongside CPMS
  until 2026-06-18 and those rows may duplicate CPMS-native sales.
* wholly zero invoices are not "missing" — there is no entry to make for them.
"""
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal

import pytest

from managementsys.accounting_checks import (
    GO_LIVE, inventory_on_hand_value, invoices_missing_from_ledger,
    non_reconciling_invoices,
)
from managementsys.models import Invoice, InvoiceItem

from .factories import InventoryBatchFactory, InventoryItemFactory, WarehouseFactory

AFTER_GO_LIVE = datetime(2026, 6, 20, 3, 0, tzinfo=dt_timezone.utc)
BEFORE_GO_LIVE = datetime(2025, 5, 20, 3, 0, tzinfo=dt_timezone.utc)


def _invoice(number, when=AFTER_GO_LIVE, total="100000", lines=(("100000", "1"),)):
    invoice = Invoice.objects.create(
        invoice_number=number, datetime=when, grand_total=Decimal(total))
    for price, qty in lines:
        InvoiceItem.objects.create(
            invoice=invoice, item_name="Light Peel",
            price=Decimal(price), quantity=Decimal(qty))
    return invoice


@pytest.mark.django_db
class TestInvoicesMissingFromLedger:
    def test_flags_a_cpms_invoice_with_no_ledger_rows(self):
        invoice = _invoice("INV-20260620-1")
        assert invoice in invoices_missing_from_ledger()

    def test_excludes_invoices_before_go_live(self):
        invoice = _invoice("INV-20250520-1", when=BEFORE_GO_LIVE)
        assert invoice not in invoices_missing_from_ledger()

    def test_excludes_ipos_imports_even_after_go_live(self):
        """The parallel-run rows — the expensive mistake to make here."""
        invoice = _invoice("IPOS-4045/KSR/GD/0626")
        assert invoice not in invoices_missing_from_ledger()
        assert invoice in invoices_missing_from_ledger(include_imported=True)

    def test_excludes_a_wholly_zero_invoice(self):
        empty = _invoice("INV-20260620-2", total="0", lines=())
        zero_lines = _invoice("INV-20260620-3", total="0", lines=(("0", "1"),))
        missing = invoices_missing_from_ledger()
        assert empty not in missing
        assert zero_lines not in missing

    def test_still_flags_an_invoice_discounted_to_zero(self):
        """grand_total 0 with real lines needs Dr discount / Cr revenue."""
        invoice = _invoice("INV-20260620-4", total="0", lines=(("300000", "1"),))
        assert invoice in invoices_missing_from_ledger()

    def test_excludes_voided_invoices(self):
        invoice = _invoice("INV-20260620-5")
        invoice.is_voided = True
        invoice.save(update_fields=["is_voided"])
        assert invoice not in invoices_missing_from_ledger()

    def test_go_live_boundary_is_the_documented_date(self):
        assert GO_LIVE == date(2026, 6, 5)


@pytest.mark.django_db
class TestNonReconcilingInvoices:
    def test_reports_a_line_total_that_disagrees_with_grand_total(self):
        # 300,000 of lines charged as 240,000 — the WinUI discount_pct bug.
        _invoice("INV-20260620-6", total="240000", lines=(("300000", "1"),))
        found = non_reconciling_invoices()
        assert [gap for _inv, gap in found] == [Decimal("60000")]

    def test_silent_when_a_line_discount_explains_the_difference(self):
        invoice = Invoice.objects.create(
            invoice_number="INV-20260620-7", datetime=AFTER_GO_LIVE,
            grand_total=Decimal("240000"))
        InvoiceItem.objects.create(
            invoice=invoice, item_name="Light Peel", price=Decimal("300000"),
            quantity=Decimal("1"), discount_pct=Decimal("20"))
        assert non_reconciling_invoices() == []


@pytest.mark.django_db
def test_inventory_on_hand_prorates_partly_consumed_batches():
    """``value`` is the batch total since migration 0074, not a unit cost."""
    warehouse = WarehouseFactory()
    item = InventoryItemFactory()
    InventoryBatchFactory(
        item=item, warehouse=warehouse,
        quantity_initial=Decimal("100"), quantity_remaining=Decimal("25"),
        value=Decimal("400000"),
    )
    assert inventory_on_hand_value() == Decimal("100000.00")
