"""Stock snapshots: /api/inventory/snapshots/.

The property worth defending is that a snapshot is **frozen**: whatever happens
to the batches or the item master afterwards, the snapshot still reports what
was held, at what value, under what name, on the day it was taken. The second is
that the GL comparison explains the known timing gaps and nothing else.
"""
import datetime
import io
from decimal import Decimal

import openpyxl
import pytest
from django.core.management import call_command
from django.urls import reverse

from managementsys.models import LedgerEntry, StockOutLog, StockSnapshot

from .factories import InventoryBatchFactory, InventoryItemFactory, WarehouseFactory


@pytest.fixture
def manager_api(auth_api, auth_user):
    auth_user.role = 'manager'
    auth_user.save(update_fields=['role'])
    return auth_api


@pytest.fixture
def super_api(auth_api, auth_user):
    auth_user.role = 'superuser'
    auth_user.save(update_fields=['role'])
    return auth_api


def _take(api, **body):
    res = api.post(reverse('inventory-snapshots'), body, format='json')
    assert res.status_code == 201, res.content
    return res.json()


def _detail(api, snap_id, **params):
    res = api.get(reverse('inventory-snapshot-detail', args=[snap_id]), params)
    assert res.status_code == 200, res.content
    return res.json()


def _row(detail, code):
    return next(r for r in detail['results'] if r['code'] == code)


def _gl(account, amount, entry_type='debit'):
    LedgerEntry.objects.create(account=account, date=datetime.date.today(),
                               description='test', entry_type=entry_type, amount=amount)


def test_requires_manager_or_superuser(auth_api, stock):
    # auth_user is a cashier: stock value is balance-sheet data.
    assert auth_api.get(reverse('inventory-snapshots')).status_code == 403
    assert auth_api.post(reverse('inventory-snapshots'), {}, format='json').status_code == 403


def test_values_remaining_stock_at_its_purchase_cost(manager_api, stock):
    stock['batch'].quantity_remaining = Decimal('40')   # 100 bought for 500 000
    stock['batch'].save()

    snap = _take(manager_api)
    row = _row(_detail(manager_api, snap['id']), stock['item'].code)

    assert Decimal(row['quantity']) == Decimal('40')
    assert Decimal(row['value']) == Decimal('200000')
    assert Decimal(row['unit_cost']) == Decimal('5000')
    assert Decimal(snap['total_value']) == Decimal('200000')


def test_lists_the_whole_master_but_never_services(manager_api, stock):
    empty = InventoryItemFactory(name='Belum pernah dibeli')
    service = InventoryItemFactory(is_service=True)

    detail = _detail(manager_api, _take(manager_api)['id'])
    codes = {r['code'] for r in detail['results']}

    assert {stock['item'].code, empty.code} <= codes
    assert service.code not in codes
    assert Decimal(_row(detail, empty.code)['quantity']) == 0
    assert detail['snapshot']['item_count'] == 2
    assert detail['snapshot']['items_in_stock'] == 1


def test_is_frozen_against_later_stock_and_master_changes(manager_api, stock):
    snap = _take(manager_api)
    item = stock['item']
    old_name = item.name

    stock['batch'].quantity_remaining = Decimal('0')
    stock['batch'].save()
    item.name = 'Nama baru'
    item.selling_price = 99999
    item.save()

    row = _row(_detail(manager_api, snap['id']), item.code)
    assert Decimal(row['quantity']) == Decimal('100')
    assert Decimal(row['value']) == Decimal('500000')
    assert row['name'] == old_name
    assert Decimal(row['selling_price']) == Decimal('10000')


def test_per_warehouse_lines_fold_into_one_item_row(manager_api, stock):
    other = WarehouseFactory(name='Gudang Cabang')
    InventoryBatchFactory(item=stock['item'], warehouse=other, quantity_initial=10,
                          quantity_remaining=10, value=Decimal('80000'))

    snap_id = _take(manager_api)['id']
    row = _row(_detail(manager_api, snap_id), stock['item'].code)
    assert Decimal(row['quantity']) == Decimal('110')
    assert Decimal(row['value']) == Decimal('580000')
    assert len(row['warehouses']) == 2

    only_other = _row(_detail(manager_api, snap_id, warehouse_id=other.id), stock['item'].code)
    assert Decimal(only_other['quantity']) == Decimal('10')
    assert Decimal(only_other['value']) == Decimal('80000')


def test_matches_the_gl_when_batches_and_ledger_agree(manager_api, stock, gl_accounts):
    _gl(gl_accounts['inventory_asset'], Decimal('500000'))

    snap = _take(manager_api)
    assert Decimal(snap['gl_balance']) == Decimal('500000')
    assert Decimal(snap['difference']) == 0
    assert Decimal(snap['unexplained_difference']) == 0


def test_an_unposted_stock_out_is_an_explained_gap(manager_api, stock, gl_accounts):
    _gl(gl_accounts['inventory_asset'], Decimal('500000'))
    # Stock left the batch (cost 50 000) but the journal run has not credited 1300000.
    stock['batch'].quantity_remaining = Decimal('90')
    stock['batch'].save()
    StockOutLog.objects.create(item=stock['item'], warehouse=stock['warehouse'],
                               out_date=datetime.date.today(), quantity=10,
                               reason=StockOutLog.REASON_EXPIRED, value=Decimal('50000'),
                               posting_status='unposted')

    snap = _take(manager_api)
    assert Decimal(snap['difference']) == Decimal('-50000')
    assert Decimal(snap['unposted_stock_out_value']) == Decimal('50000')
    assert Decimal(snap['unexplained_difference']) == 0


def test_a_real_discrepancy_is_left_unexplained(manager_api, stock, gl_accounts):
    _gl(gl_accounts['inventory_asset'], Decimal('650000'))

    snap = _take(manager_api)
    assert Decimal(snap['unexplained_difference']) == Decimal('-150000')


def test_no_ledger_rows_means_no_gl_figure_not_zero(manager_api, stock):
    snap = _take(manager_api)
    assert snap['gl_balance'] is None
    assert snap['unexplained_difference'] is None


def test_compare_reports_the_change_per_item(manager_api, stock):
    first = _take(manager_api)['id']
    stock['batch'].quantity_remaining = Decimal('70')
    stock['batch'].save()
    second = _take(manager_api)['id']

    row = _row(_detail(manager_api, second, compare_to=first), stock['item'].code)
    assert Decimal(row['base_quantity']) == Decimal('100')
    assert Decimal(row['delta_quantity']) == Decimal('-30')
    assert Decimal(row['delta_value']) == Decimal('-150000')


def test_compare_to_an_unknown_snapshot_is_a_400(manager_api, stock):
    snap = _take(manager_api)['id']
    res = manager_api.get(reverse('inventory-snapshot-detail', args=[snap]), {'compare_to': 999999})
    assert res.status_code == 400


def test_list_filters_by_date(manager_api, stock):
    snap = _take(manager_api)
    today = snap['snapshot_date']
    res = manager_api.get(reverse('inventory-snapshots'), {'date_from': today, 'date_to': today})
    assert [s['id'] for s in res.json()['results']] == [snap['id']]
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    res = manager_api.get(reverse('inventory-snapshots'), {'date_to': yesterday})
    assert res.json()['results'] == []


def test_export_is_a_workbook_with_the_master_list(manager_api, stock):
    first = _take(manager_api)['id']
    second = _take(manager_api)['id']
    res = manager_api.get(reverse('inventory-snapshot-export', args=[second]),
                          {'compare_to': first})
    assert res.status_code == 200
    assert res['Content-Type'].startswith('application/vnd.openxmlformats')
    wb = openpyxl.load_workbook(io.BytesIO(res.content))
    assert wb.sheetnames == ['Ringkasan', 'Per Item', 'Per Gudang']
    values = [c for row in wb['Per Item'].iter_rows(values_only=True) for c in row]
    assert stock['item'].code in values
    assert 'Δ Nilai' in values


def test_only_superuser_may_delete(manager_api, auth_user, stock):
    snap = _take(manager_api)['id']
    url = reverse('inventory-snapshot-detail', args=[snap])
    assert manager_api.delete(url).status_code == 403

    auth_user.role = 'superuser'
    auth_user.save(update_fields=['role'])
    assert manager_api.delete(url).status_code == 204
    assert not StockSnapshot.objects.filter(pk=snap).exists()


def test_command_once_per_day_keeps_one_automatic_snapshot(stock):
    call_command('take_stock_snapshot', '--once-per-day', stdout=io.StringIO())
    call_command('take_stock_snapshot', '--once-per-day', stdout=io.StringIO())
    assert StockSnapshot.objects.filter(source='auto').count() == 1


def test_command_dry_run_saves_nothing(stock):
    out = io.StringIO()
    call_command('take_stock_snapshot', '--dry-run', stdout=out)
    assert 'nothing saved' in out.getvalue()
    assert not StockSnapshot.objects.exists()
