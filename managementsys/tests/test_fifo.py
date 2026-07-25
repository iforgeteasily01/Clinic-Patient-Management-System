"""Tests for FIFO stock deduction (managementsys.views.inventory_page._fifo_deduct).

Two complementary styles:
  * ``TestFifoDeductMocked`` mocks the InventoryBatch queryset to verify the pure
    ordering/COGS algorithm in isolation from real rows. (_fifo_deduct is wrapped
    in @transaction.atomic, so an active DB connection is still required, hence
    @pytest.mark.django_db — but no real batches are created.)
  * ``TestFifoDeductRealDB`` exercises the same function against real
    InventoryBatch rows to prove the ORM query + save path behaves as expected.
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from managementsys.views.inventory_page import _fifo_deduct
from .factories import InventoryBatchFactory, InventoryItemFactory, WarehouseFactory


def _fake_batch(remaining, value, initial):
    """A stand-in for an InventoryBatch row that records writes to quantity_remaining."""
    b = MagicMock()
    b.quantity_remaining = Decimal(remaining)
    b.value = Decimal(value)
    b.quantity_initial = Decimal(initial)
    return b


@pytest.mark.django_db
class TestFifoDeductMocked:
    def _wire(self, MockBatch, batches):
        # Mirrors the call chain in the view:
        #   InventoryBatch.objects.select_for_update().filter(...).order_by(...)
        chain = MockBatch.objects.select_for_update.return_value.filter.return_value
        chain.order_by.return_value = batches

    @patch("managementsys.views.inventory_page.InventoryBatch")
    def test_spans_two_batches_with_blended_cogs(self, MockBatch):
        batch_a = _fake_batch(remaining=10, value=50000, initial=10)   # 5000/unit
        batch_b = _fake_batch(remaining=10, value=80000, initial=10)   # 8000/unit
        self._wire(MockBatch, [batch_a, batch_b])

        shortfall, cogs = _fifo_deduct(item_id=1, warehouse_id=1, quantity=Decimal("15"))

        assert shortfall == Decimal("0")
        # 10 @ 5000 + 5 @ 8000 = 50000 + 40000
        assert cogs == Decimal("90000")
        assert batch_a.quantity_remaining == Decimal("0")   # oldest drained first
        assert batch_b.quantity_remaining == Decimal("5")

    @patch("managementsys.views.inventory_page.InventoryBatch")
    def test_reports_shortfall_when_understocked(self, MockBatch):
        batch_a = _fake_batch(remaining=3, value=15000, initial=3)
        self._wire(MockBatch, [batch_a])

        shortfall, cogs = _fifo_deduct(item_id=1, warehouse_id=1, quantity=Decimal("5"))

        assert shortfall == Decimal("2")     # 2 units could not be sourced
        assert cogs == Decimal("15000")      # COGS only for the 3 real units
        assert batch_a.quantity_remaining == Decimal("0")


@pytest.mark.django_db
class TestFifoDeductRealDB:
    def test_oldest_batch_consumed_first(self):
        item = InventoryItemFactory()
        wh = WarehouseFactory()
        old = InventoryBatchFactory(
            item=item, warehouse=wh, input_date="2026-01-01",
            quantity_initial=Decimal("10"), quantity_remaining=Decimal("10"),
            value=Decimal("50000"),   # 5000/unit
        )
        new = InventoryBatchFactory(
            item=item, warehouse=wh, input_date="2026-02-01",
            quantity_initial=Decimal("10"), quantity_remaining=Decimal("10"),
            value=Decimal("80000"),   # 8000/unit
        )

        shortfall, cogs = _fifo_deduct(item.id, wh.id, Decimal("12"))

        old.refresh_from_db()
        new.refresh_from_db()
        assert shortfall == Decimal("0")
        assert old.quantity_remaining == Decimal("0")    # fully drained
        assert new.quantity_remaining == Decimal("8")    # 2 taken from newer
        # 10 @ 5000 + 2 @ 8000
        assert cogs == Decimal("66000")

    def test_selling_the_last_unit_twice_never_oversells(self):
        """EDGE CASE (contention): two deductions of the final unit.

        Deterministic stand-in for the concurrent-checkout race. The first call
        takes the unit; the second finds nothing and reports a full shortfall.
        Stock never goes negative.
        """
        item = InventoryItemFactory()
        wh = WarehouseFactory()
        batch = InventoryBatchFactory(
            item=item, warehouse=wh,
            quantity_initial=Decimal("1"), quantity_remaining=Decimal("1"),
            value=Decimal("5000"),
        )

        first_shortfall, _ = _fifo_deduct(item.id, wh.id, Decimal("1"))
        second_shortfall, second_cogs = _fifo_deduct(item.id, wh.id, Decimal("1"))

        batch.refresh_from_db()
        assert first_shortfall == Decimal("0")
        assert second_shortfall == Decimal("1")     # nothing left to take
        assert second_cogs == Decimal("0")
        assert batch.quantity_remaining == Decimal("0")
