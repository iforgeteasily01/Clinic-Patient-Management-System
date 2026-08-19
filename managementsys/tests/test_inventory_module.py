"""Tests for the redesigned inventory module.

Two behaviours worth pinning down, both of them about the boundary between
inventory and the general ledger:

* **Stock-in through the inventory menu carries no value.** It writes no journal,
  so a value entered here would grow the balance sheet with no credit anywhere
  and then leak into the P&L later as FIFO COGS. Priced stock comes through
  Purchasing, which posts the other side.
* **Stock-out is charged by reason.** The reason picks the account, the FIFO cost
  is captured at deduction time, and the row is born `unposted` so the journal
  sweep picks it up — except for a transfer, which has nothing to charge.

The dashboard tests cover the low-stock classification the module hub renders.
"""
from decimal import Decimal

import pytest

from managementsys.models import InventoryBatch, StockOutLog
from managementsys.views.inventory_page import _stock_status
from .factories import InventoryBatchFactory, InventoryItemFactory, WarehouseFactory


# ── Stock in: always zero value ─────────────────────────────────────────────

@pytest.mark.django_db
class TestStockInIsValueless:
    def test_single_entry_creates_zero_value_batch(self, auth_api):
        item = InventoryItemFactory()
        warehouse = WarehouseFactory()

        res = auth_api.post('/api/inventory/stock-in/', {
            'item_id': item.id,
            'warehouse_id': warehouse.id,
            'input_date': '2026-08-19',
            'quantity': 10,
            'unit': 'small',
        }, format='json')

        assert res.status_code == 201
        batch = InventoryBatch.objects.get(item=item)
        assert batch.quantity_initial == Decimal('10')
        assert batch.value == Decimal('0')

    def test_value_in_the_payload_is_ignored(self, auth_api):
        """A caller written against the old API must not be able to set a value.

        The endpoint used to require one. Silently dropping it is deliberate:
        rejecting the request would break the POS and the Excel importer for a
        field neither of them needs any more.
        """
        item = InventoryItemFactory()
        warehouse = WarehouseFactory()

        res = auth_api.post('/api/inventory/stock-in/', {
            'item_id': item.id,
            'warehouse_id': warehouse.id,
            'input_date': '2026-08-19',
            'quantity': 5,
            'value': '999000',
        }, format='json')

        assert res.status_code == 201
        assert InventoryBatch.objects.get(item=item).value == Decimal('0')

    def test_bulk_entries_are_zero_valued_too(self, auth_api):
        item_a = InventoryItemFactory()
        item_b = InventoryItemFactory()
        warehouse = WarehouseFactory()

        res = auth_api.post('/api/inventory/stock-in/', {
            'entries': [
                {'item_id': item_a.id, 'warehouse_id': warehouse.id,
                 'input_date': '2026-08-19', 'quantity': 3, 'value': '111'},
                {'item_id': item_b.id, 'warehouse_id': warehouse.id,
                 'input_date': '2026-08-19', 'quantity': 4},
            ],
        }, format='json')

        assert res.status_code == 201
        assert res.json()['imported'] == 2
        assert list(InventoryBatch.objects.values_list('value', flat=True)) == [
            Decimal('0'), Decimal('0'),
        ]


# ── Stock out: reason drives the account ────────────────────────────────────

@pytest.mark.django_db
class TestStockOutJournalling:
    def test_expired_issue_is_unposted_with_fifo_cost(self, auth_api, stock):
        res = auth_api.post('/api/inventory/stock-out/', {
            'item_id': stock['item'].id,
            'warehouse_id': stock['warehouse'].id,
            'quantity': 10,
            'reason': StockOutLog.REASON_EXPIRED,
        }, format='json')

        assert res.status_code == 200
        log = StockOutLog.objects.get()
        # 10 units drawn from a batch costing 5000/unit.
        assert log.value == Decimal('50000')
        assert log.expense_account_number == 5000900
        assert log.posting_status == 'unposted'
        assert log.is_journalable

    def test_transfer_posts_nothing_and_is_born_posted(self, auth_api, stock):
        """A warehouse transfer moves stock the clinic still owns.

        Charging it to an expense would take the value off the balance sheet
        twice, so it gets no account and is marked posted up front rather than
        being reconsidered by every future journal run.
        """
        res = auth_api.post('/api/inventory/stock-out/', {
            'item_id': stock['item'].id,
            'warehouse_id': stock['warehouse'].id,
            'quantity': 4,
            'reason': StockOutLog.REASON_TRANSFER,
        }, format='json')

        assert res.status_code == 200
        log = StockOutLog.objects.get()
        assert log.expense_account_number is None
        assert log.posting_status == 'posted'
        assert not log.is_journalable

    def test_zero_cost_stock_is_posted_with_nothing_to_charge(self, auth_api):
        """Stock that came in through the inventory menu costs nothing to issue.

        This is the downstream half of the zero-value stock-in rule: the clinic
        never recorded paying for the goods, so writing them off charges nothing
        and there is no entry to make.
        """
        item = InventoryItemFactory()
        warehouse = WarehouseFactory()
        InventoryBatchFactory(
            item=item, warehouse=warehouse,
            quantity_initial=Decimal('20'), quantity_remaining=Decimal('20'),
            value=Decimal('0'),
        )

        res = auth_api.post('/api/inventory/stock-out/', {
            'item_id': item.id,
            'warehouse_id': warehouse.id,
            'quantity': 5,
            'reason': StockOutLog.REASON_DAMAGED,
        }, format='json')

        assert res.status_code == 200
        log = StockOutLog.objects.get()
        assert log.value == Decimal('0')
        assert log.posting_status == 'posted'

    def test_unknown_reason_is_rejected(self, auth_api, stock):
        res = auth_api.post('/api/inventory/stock-out/', {
            'item_id': stock['item'].id,
            'warehouse_id': stock['warehouse'].id,
            'quantity': 1,
            'reason': 'because',
        }, format='json')

        assert res.status_code == 400
        assert 'Unknown reason' in res.json()['error']
        assert not StockOutLog.objects.exists()


@pytest.mark.django_db
def test_stock_out_reasons_endpoint_lists_every_choice(auth_api, gl_accounts):
    res = auth_api.get('/api/inventory/stock-out/reasons/')

    assert res.status_code == 200
    rows = {r['code']: r for r in res.json()}
    assert set(rows) == {code for code, _ in StockOutLog.REASON_CHOICES}
    assert rows[StockOutLog.REASON_EXPIRED]['account_number'] == 5000900
    assert rows[StockOutLog.REASON_EXPIRED]['posts_journal'] is True
    assert rows[StockOutLog.REASON_TRANSFER]['account_number'] is None
    assert rows[StockOutLog.REASON_TRANSFER]['posts_journal'] is False


# ── Dashboard: low-stock classification ─────────────────────────────────────

class TestStockStatus:
    """`_stock_status` is the rule the whole alert surface rests on."""

    @pytest.mark.parametrize('qty, minimum, expected', [
        (Decimal('4'),  Decimal('10'), 'below'),
        (Decimal('0'),  Decimal('10'), 'below'),
        (Decimal('10'), Decimal('10'), 'approaching'),   # exactly at the minimum
        (Decimal('12'), Decimal('10'), 'approaching'),   # the 1.2× band edge
        (Decimal('13'), Decimal('10'), 'ok'),
        # No minimum set means no threshold to breach — including at zero, where
        # flagging every discontinued item would bury the ones that matter.
        (Decimal('0'),  Decimal('0'),  'ok'),
    ])
    def test_bands(self, qty, minimum, expected):
        assert _stock_status(qty, minimum) == expected


@pytest.mark.django_db
class TestInventoryDashboard:
    def test_counts_and_alerts_aggregate_across_warehouses(self, auth_api):
        """Stock is judged per item, not per warehouse row.

        An item split 4/4 over two warehouses against a minimum of 5 is fine.
        The per-warehouse stock table would call both rows low; the hub must not,
        or the headline count overstates how much needs reordering.
        """
        wh_a, wh_b = WarehouseFactory(), WarehouseFactory()
        split = InventoryItemFactory(min_stock=5)
        InventoryBatchFactory(item=split, warehouse=wh_a,
                              quantity_initial=Decimal('4'), quantity_remaining=Decimal('4'),
                              value=Decimal('4000'))
        InventoryBatchFactory(item=split, warehouse=wh_b,
                              quantity_initial=Decimal('4'), quantity_remaining=Decimal('4'),
                              value=Decimal('4000'))

        short = InventoryItemFactory(min_stock=20)
        InventoryBatchFactory(item=short, warehouse=wh_a,
                              quantity_initial=Decimal('2'), quantity_remaining=Decimal('2'),
                              value=Decimal('2000'))

        res = auth_api.get('/api/inventory/dashboard/')

        assert res.status_code == 200
        data = res.json()
        assert data['counts']['below_minimum'] == 1
        assert data['counts']['total_items'] == 2
        codes = [a['item_code'] for a in data['alerts']]
        assert codes == [short.code]          # the split item is not an alert
        assert data['alerts'][0]['gap'].startswith('-18')

    def test_worst_shortfall_is_listed_first(self, auth_api):
        warehouse = WarehouseFactory()
        mild = InventoryItemFactory(min_stock=10)
        InventoryBatchFactory(item=mild, warehouse=warehouse,
                              quantity_initial=Decimal('9'), quantity_remaining=Decimal('9'),
                              value=Decimal('900'))
        severe = InventoryItemFactory(min_stock=100)
        InventoryBatchFactory(item=severe, warehouse=warehouse,
                              quantity_initial=Decimal('1'), quantity_remaining=Decimal('1'),
                              value=Decimal('100'))
        # Inside the warning band, so it must rank behind both 'below' rows.
        warning = InventoryItemFactory(min_stock=10)
        InventoryBatchFactory(item=warning, warehouse=warehouse,
                              quantity_initial=Decimal('11'), quantity_remaining=Decimal('11'),
                              value=Decimal('1100'))

        data = auth_api.get('/api/inventory/dashboard/').json()

        assert [a['item_code'] for a in data['alerts']] == [severe.code, mild.code, warning.code]
        assert data['counts']['below_minimum'] == 2
        assert data['counts']['approaching_minimum'] == 1

    def test_reports_unposted_stock_out_backlog(self, auth_api, stock):
        auth_api.post('/api/inventory/stock-out/', {
            'item_id': stock['item'].id,
            'warehouse_id': stock['warehouse'].id,
            'quantity': 1,
            'reason': StockOutLog.REASON_EXPIRED,
        }, format='json')

        assert auth_api.get('/api/inventory/dashboard/').json()['unposted_stock_out'] == 1
