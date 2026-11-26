"""Stock snapshots: the master item list with quantity and value, frozen at one instant.

Why stored and not reconstructed
--------------------------------
``InventoryBatch`` holds only the present. Rebuilding a past date would mean
replaying every movement through FIFO, and the inputs for that are incomplete:
the kartu stok (``stock_card.py``) does not see treatment-material draws, an
iPos import draws only when the ledger says so, a restock refills newest-first,
and there is no per-batch consumption record to say which batch a past sale
actually hit. The value such a replay produced would be an estimate, which is no
use for checking whether the value *matches*. A snapshot copies the batches
as they are, so its figures are exact for the moment it was taken, and only
dates on which one was taken can be looked at.

What "matches" means here
-------------------------
Batches and the GL inventory account (1300000) move together for sales and
returns: both change when the journal run posts the document. Two documents move
them at different times, and both are captured so the gap can be explained:

* a **purchase** creates valued batches when it is saved, but debits 1300000
  only when the journal run posts it → ``unposted_purchase_value``;
* a **stock-out** reduces the batches when it is saved, but credits 1300000
  only when the run posts it → ``unposted_stock_out_value``.

``expected GL = GL balance + unposted purchases - unposted stock-outs``, and
whatever separates that from the batch value is a real discrepancy, shown as
``unexplained_difference``. It is computed for display and never stored, so the
rule can be refined without migrating old rows.

Batches and the ledger are read by separate queries, and under PostgreSQL's
default READ COMMITTED each query sees its own moment — a journal run committing
between them would skew the comparison by exactly the documents it posted. So
``take_snapshot`` raises its transaction to REPEATABLE READ when it owns the
transaction, which makes every read see the same instant. Nested inside someone
else's transaction (a test, or a future caller) it cannot change the level and
reads at whatever that transaction uses.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from ..models import (
    JAKARTA_TZ,
    InventoryBatch,
    InventoryItem,
    LedgerEntry,
    StockOutLog,
    StockSnapshot,
    StockSnapshotLine,
)
from .journal_engine import INVENTORY_ASSET_NUMBER
from .journal_sweep import JOURNALABLE_STOCK_REASONS

ZERO = Decimal('0')
CENT = Decimal('0.01')
QTY = Decimal('0.0001')


def gl_inventory_balance():
    """Debit-minus-credit of every ledger row ever written to account 1300000.

    Not ``ChartOfAccounts.balance``: that column is a running total maintained
    alongside the ledger and has been out of step with it before (see the
    ledger-balance repair). The ledger rows are the record.
    """
    agg = (LedgerEntry.objects
           .filter(account__account_number=INVENTORY_ASSET_NUMBER)
           .aggregate(d=Sum('amount', filter=Q(entry_type='debit')),
                      c=Sum('amount', filter=Q(entry_type='credit')),
                      n=Count('id')))
    if not agg['n']:
        return None
    return (agg['d'] or ZERO) - (agg['c'] or ZERO)


def unposted_purchase_value():
    """Value sitting in batches whose purchase invoice the journal has not posted."""
    return (InventoryBatch.objects
            .filter(purchase_invoice__posting_status='unposted',
                    purchase_invoice__is_voided=False)
            .aggregate(t=Sum('value'))['t'] or ZERO)


def unposted_stock_out_value():
    """FIFO cost already taken out of batches by stock-outs not yet posted."""
    return (StockOutLog.objects
            .filter(posting_status='unposted', value__gt=0,
                    reason__in=JOURNALABLE_STOCK_REASONS)
            .aggregate(t=Sum('value'))['t'] or ZERO)


def _batch_totals():
    """{(item_id, warehouse_id): (qty, value, batch_count)} over batches with stock."""
    # The one valuation rule, shared with StockLevelView. Lazy because the views
    # package imports this module.
    from ..views.inventory_page import _batch_value_expr
    rows = (InventoryBatch.objects
            .filter(quantity_remaining__gt=0)
            .values('item_id', 'warehouse_id', 'warehouse__name')
            .annotate(qty=Sum('quantity_remaining'), val=Sum(_batch_value_expr()),
                      n=Count('id')))
    return {(r['item_id'], r['warehouse_id']): r for r in rows}


def capture():
    """Read the current state without writing anything.

    Returns ``(header_fields, line_dicts)``. Split from ``take_snapshot`` so a
    dry run (``take_stock_snapshot --dry-run``) shows exactly what would be kept.
    """
    now = timezone.now()
    items = list(InventoryItem.objects
                 .filter(is_service=False)
                 .select_related('item_category')
                 .order_by('code'))
    totals = _batch_totals()
    by_item = defaultdict(list)
    for (item_id, _wh), r in totals.items():
        by_item[item_id].append(r)

    lines = []
    total_qty = ZERO
    total_value = ZERO
    in_stock = 0
    for it in items:
        master = {
            'item_id': it.id,
            'item_code': it.code,
            'item_name': it.name,
            'category': it.item_category.name if it.item_category_id else (it.category or ''),
            'unit_small': it.unit_small,
            'selling_price': it.selling_price,
            'min_stock': it.min_stock,
            'is_active': it.is_active,
        }
        rows = sorted(by_item.get(it.id, []), key=lambda r: r['warehouse__name'] or '')
        if not rows:
            lines.append({**master, 'warehouse_id': None, 'warehouse_name': '',
                          'quantity': ZERO, 'value': ZERO, 'batch_count': 0})
            continue
        in_stock += 1
        for r in rows:
            qty = Decimal(r['qty'] or 0).quantize(QTY)
            val = Decimal(r['val'] or 0).quantize(CENT)
            total_qty += qty
            total_value += val
            lines.append({**master, 'warehouse_id': r['warehouse_id'],
                          'warehouse_name': r['warehouse__name'] or '',
                          'quantity': qty, 'value': val, 'batch_count': r['n']})

    gl = gl_inventory_balance()
    header = {
        'snapshot_date': now.astimezone(JAKARTA_TZ).date(),
        'taken_at': now,
        'item_count': len(items),
        'items_in_stock': in_stock,
        'total_quantity': total_qty,
        'total_value': total_value,
        'gl_inventory_balance': gl.quantize(CENT) if gl is not None else None,
        'unposted_purchase_value': unposted_purchase_value().quantize(CENT),
        'unposted_stock_out_value': unposted_stock_out_value().quantize(CENT),
    }
    return header, lines


def take_snapshot(*, user=None, source=StockSnapshot.SOURCE_MANUAL, notes='') -> StockSnapshot:
    owns_transaction = not connection.in_atomic_block
    with transaction.atomic():
        if owns_transaction and connection.vendor == 'postgresql':
            # Must be the first statement of the transaction to take effect.
            with connection.cursor() as cur:
                cur.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        header, lines = capture()
        snap = StockSnapshot.objects.create(taken_by=user, source=source,
                                            notes=notes or '', **header)
        StockSnapshotLine.objects.bulk_create(
            [StockSnapshotLine(snapshot=snap, **ln) for ln in lines], batch_size=1000,
        )
    return snap


# ── Reading ────────────────────────────────────────────────────────────────

def reconciliation(snap: StockSnapshot) -> dict:
    """Batch value vs GL, with the two timing differences taken out."""
    gl = snap.gl_inventory_balance
    if gl is None:
        return {'gl_balance': None, 'difference': None,
                'expected_gl': None, 'unexplained_difference': None}
    expected = gl + snap.unposted_purchase_value - snap.unposted_stock_out_value
    return {
        'gl_balance': gl,
        'difference': snap.total_value - gl,
        'expected_gl': expected,
        'unexplained_difference': snap.total_value - expected,
    }


def item_rows(snap: StockSnapshot, *, warehouse_id=None, q='') -> list[dict]:
    """One row per item: lines folded across warehouses (or cut to one warehouse).

    Under a warehouse filter an item with no stock there still appears with
    zero, so the list stays a full master list and the comparison column can
    show stock that *left* that warehouse.
    """
    lines = snap.lines.all()
    if q:
        lines = lines.filter(Q(item_code__icontains=q) | Q(item_name__icontains=q))
    rows: dict = {}
    for ln in lines.order_by('item_code', 'warehouse_name'):
        key = ln.item_id or f'code:{ln.item_code}'
        row = rows.get(key)
        if row is None:
            row = rows[key] = {
                'item_id': ln.item_id, 'code': ln.item_code, 'name': ln.item_name,
                'category': ln.category, 'unit': ln.unit_small,
                'selling_price': ln.selling_price, 'min_stock': ln.min_stock,
                'is_active': ln.is_active, 'quantity': ZERO, 'value': ZERO,
                'warehouses': [],
            }
        if warehouse_id is not None and ln.warehouse_id != warehouse_id:
            continue
        if ln.warehouse_id is not None:
            row['quantity'] += ln.quantity
            row['value'] += ln.value
            row['warehouses'].append({'id': ln.warehouse_id, 'name': ln.warehouse_name,
                                      'quantity': ln.quantity, 'value': ln.value})
    for row in rows.values():
        row['unit_cost'] = (row['value'] / row['quantity']).quantize(CENT) if row['quantity'] else None
    return list(rows.values())


def compare(rows: list[dict], base_rows: list[dict]) -> list[dict]:
    """Attach the earlier snapshot's figures and the change to each row.

    Items are matched on id (falling back to code for an item since deleted).
    An item present only in the base snapshot is appended so stock that vanished
    along with its item is not silently dropped.
    """
    def key(r):
        return r['item_id'] or f'code:{r["code"]}'

    base = {key(r): r for r in base_rows}
    seen = set()
    for r in rows:
        b = base.get(key(r))
        seen.add(key(r))
        r['base_quantity'] = b['quantity'] if b else ZERO
        r['base_value'] = b['value'] if b else ZERO
        r['delta_quantity'] = r['quantity'] - r['base_quantity']
        r['delta_value'] = r['value'] - r['base_value']
    for k, b in base.items():
        if k in seen:
            continue
        rows.append({**b, 'quantity': ZERO, 'value': ZERO, 'unit_cost': None, 'warehouses': [],
                     'base_quantity': b['quantity'], 'base_value': b['value'],
                     'delta_quantity': -b['quantity'], 'delta_value': -b['value'],
                     'missing_now': True})
    return rows
