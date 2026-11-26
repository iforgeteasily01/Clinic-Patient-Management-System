"""Kartu stok: every movement of one inventory item, read from its source documents.

There is no movement table. Stock is held in ``InventoryBatch`` rows and every
change to them is made by some document — a sale, a return, a purchase, a
stock-out, an opname, a production run, a pencacahan. Rather than add a ledger
that would only start filling from the day it shipped, this module asks each of
those documents what it did to the item. The history is therefore complete back
to the first record, and it cannot drift from the documents because it *is* the
documents.

Four things a reader needs to know:

* **An imported iPos sale moved stock only if the ledger says so.** ``IPOS-*``
  invoices (``accounting_checks.IMPORTED_INVOICE_PREFIX``) were imported as
  history and nearly all of them never drew on a CPMS batch — the opening stock
  was keyed in already net of them. But a journal run on 2026-08-06 did post
  ~250 of them, and those *did* consume real batches. ``posting_status`` cannot
  tell the two apart (all say ``posted``), so the evidence used is the invoice's
  own ``FIFO deduction: <item name>`` ledger row, net of any ``FIFO restock``.
  Such a row is dated when its journal entry was created — the day the stock
  actually left the batch — with the invoice's own date kept in the note.
  Caveat: a draw on a zero-value batch writes no ledger row, and a renamed item
  no longer matches its old descriptions; both would show as ``not_deducted``.
* **Not every movement has happened yet.** A sale or a return moves stock when
  the journal run posts it, not when it is saved (see ``build_invoice_legs`` and
  ``sales_returns.build_return_legs``). Such rows are ``pending``. A sale that
  will never move stock — an ``excluded`` invoice, or one with no warehouse — is
  ``not_deducted``. Only ``applied`` rows count toward the running balance.
* **The running balance is a reconstruction, and it is checked.** It is compared
  with what the batches actually hold now. They can legitimately differ: a sale
  larger than the stock on hand deducts only what was there (the FIFO shortfall
  is dropped), and a restock is capped at each batch's original size. The gap is
  reported, never hidden.
* **Two kinds of batch are matched, not linked.** A pencacahan's target batch
  has no foreign key back to its record, and old production runs may have lost
  ``produced_batch`` to SET_NULL. Both are matched on item, warehouse, date,
  quantity, value and a creation time within ``_MATCH_WINDOW`` of the record —
  the view creates the batch and the record in the same request. A matched batch
  is shown once, as the pencacahan/production row, not again as a stock-in.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from decimal import Decimal

from ..accounting_checks import IMPORTED_INVOICE_PREFIX
from ..models import (
    JAKARTA_TZ,
    InventoryBatch,
    InvoiceItem,
    LedgerEntry,
    PencacahanRecord,
    ProductionRun,
    ProductionRunIngredient,
    SalesReturnItem,
    StockOpnameItem,
    StockOutLog,
)

ZERO = Decimal('0')
_MATCH_WINDOW = datetime.timedelta(minutes=2)

APPLIED = 'applied'
PENDING = 'pending'
NOT_DEDUCTED = 'not_deducted'

KIND_LABELS = {
    'sale':           'Penjualan',
    'sale_return':    'Retur Penjualan',
    'purchase':       'Pembelian',
    'stock_in':       'Stok Masuk',
    'stock_out':      'Stok Keluar',
    'opname':         'Susut Stok Opname',
    'production_in':  'Hasil Produksi',
    'production_out': 'Bahan Produksi',
    'pencacahan_in':  'Pencacahan (Masuk)',
    'pencacahan_out': 'Pencacahan (Keluar)',
}


@dataclass
class Movement:
    kind: str
    date: datetime.date
    at: datetime.datetime          # tie-break within a day
    quantity: Decimal              # signed: + in, − out
    warehouse: object | None
    doc_type: str
    doc_id: int
    doc_number: str
    status: str = APPLIED
    patient_no: str | None = None
    patient_name: str | None = None
    counterparty: str = ''
    note: str = ''
    actor: str = ''
    balance_after: Decimal | None = field(default=None)

    @property
    def sort_key(self):
        # Ins before outs at the same instant, so a same-second stock-in and
        # sale never dips the running balance below zero on paper.
        return (self.date, self.at, 0 if self.quantity > 0 else 1, self.doc_id)


def _local_date(dt):
    return dt.astimezone(JAKARTA_TZ).date() if dt else None


def _at(date, dt):
    """A sortable aware datetime for a date-only document."""
    if dt is not None:
        return dt
    return datetime.datetime.combine(date, datetime.time.min, tzinfo=JAKARTA_TZ)


def _name(user):
    return (getattr(user, 'display_name', '') or '') if user else ''


def _wh(movement_wh, warehouse_id):
    return warehouse_id is None or (movement_wh is not None and movement_wh.id == warehouse_id)


def _claim_batch(pool, *, warehouse_id, date, quantity, value, created_at):
    """Take the first unclaimed batch from ``pool`` that this document created."""
    for b in pool:
        if (b.warehouse_id == warehouse_id and b.input_date == date
                and b.quantity_initial == quantity and b.value == value
                and created_at is not None
                and abs(b.created_at - created_at) <= _MATCH_WINDOW):
            pool.remove(b)
            return b
    return None


def _imported_invoices_that_drew(item, lines) -> dict:
    """Imported (IPOS-*) invoices whose FIFO draw on this item still stands.

    Returns ``{invoice_id: when the draw happened}`` — the creation time of the
    journal entry that carried it, which is when stock actually left the batch,
    not the 2024 date printed on the invoice.
    """
    ids = {ln.invoice_id for ln in lines
           if ln.invoice.invoice_number.startswith(f'{IMPORTED_INVOICE_PREFIX}-')}
    if not ids:
        return {}
    net: dict = {}
    when: dict = {}
    rows = (LedgerEntry.objects
            .filter(invoice_id__in=ids, description__endswith=f': {item.name}')
            .values_list('invoice_id', 'description', 'journal_entry__created_at'))
    for inv_id, desc, at in rows:
        if f'FIFO deduction: {item.name}' in desc:
            net[inv_id] = net.get(inv_id, 0) + 1
            if at is not None:
                when[inv_id] = max(when.get(inv_id, at), at)
        elif f'FIFO restock: {item.name}' in desc:
            net[inv_id] = net.get(inv_id, 0) - 1
    return {i: when.get(i) for i, n in net.items() if n > 0}


def item_movements(item, *, warehouse_id: int | None = None) -> list[Movement]:
    """Every movement of ``item``, oldest first, with ``balance_after`` filled in."""
    out: list[Movement] = []

    # ── Sales ────────────────────────────────────────────────────────────────
    lines = (
        InvoiceItem.objects
        .filter(item=item, invoice__is_voided=False, quantity__gt=0)
        .select_related('invoice__patient_no', 'invoice__warehouse', 'invoice__cashier')
    )
    lines = list(lines)
    imported_drawn = _imported_invoices_that_drew(item, lines)
    for ln in lines:
        inv = ln.invoice
        imported = inv.invoice_number.startswith(f'{IMPORTED_INVOICE_PREFIX}-')
        moved_at = inv.datetime
        if imported and inv.id in imported_drawn:
            # Listed on the day the stock moved, so the running balance follows
            # the batches; the invoice's own date stays in the note.
            moved_at = imported_drawn[inv.id] or inv.datetime
            st, why = APPLIED, (f'Faktur impor iPos tgl {_local_date(inv.datetime):%d-%m-%Y}, '
                                'dijurnal belakangan dan memotong stok CPMS')
        elif imported:
            st, why = NOT_DEDUCTED, 'Impor iPos: stok awal CPMS sudah memperhitungkan penjualan ini'
        elif inv.posting_status == 'excluded':
            st, why = NOT_DEDUCTED, 'Tidak memotong stok: dicatat manual di luar CPMS'
        elif inv.warehouse_id is None:
            st, why = NOT_DEDUCTED, 'Tidak memotong stok: faktur tanpa gudang'
        elif inv.posting_status == 'posted':
            st, why = APPLIED, ''
        else:
            st, why = PENDING, ''
        patient = inv.patient_no
        out.append(Movement(
            kind='sale', date=_local_date(moved_at), at=moved_at,
            quantity=-Decimal(ln.quantity), warehouse=inv.warehouse,
            doc_type='invoice', doc_id=inv.id, doc_number=inv.invoice_number,
            status=st,
            patient_no=patient.patient_no if patient else None,
            patient_name=patient.name if patient else None,
            note=why,
            actor=_name(inv.cashier),
        ))

    # ── Sales returns ────────────────────────────────────────────────────────
    rlines = (
        SalesReturnItem.objects
        .filter(item=item, sales_return__is_voided=False, restock=True, quantity__gt=0)
        .select_related('sales_return__warehouse', 'sales_return__processed_by',
                        'sales_return__invoice__warehouse',
                        'sales_return__invoice__patient_no')
    )
    for ln in rlines:
        rtn = ln.sales_return
        wh = rtn.warehouse or rtn.invoice.warehouse
        if wh is None:
            st = NOT_DEDUCTED
        elif rtn.posting_status == 'posted':
            st = APPLIED
        else:
            st = PENDING
        patient = rtn.invoice.patient_no
        out.append(Movement(
            kind='sale_return', date=_local_date(rtn.datetime), at=rtn.datetime,
            quantity=Decimal(ln.quantity), warehouse=wh,
            doc_type='sales_return', doc_id=rtn.id, doc_number=rtn.return_number,
            status=st,
            patient_no=patient.patient_no if patient else None,
            patient_name=patient.name if patient else None,
            note=f'Retur atas {rtn.invoice.invoice_number}',
            actor=_name(rtn.processed_by),
        ))

    # ── Batches: purchases, plain stock-in, and the batches others created ──
    batches = list(
        InventoryBatch.objects.filter(item=item)
        .select_related('warehouse', 'created_by', 'purchase_invoice__supplier',
                        'purchase_invoice__created_by')
        .order_by('created_at', 'id')
    )
    unlinked = [b for b in batches if b.purchase_invoice_id is None]

    # Purchases: one row per (invoice, warehouse). An edit after a sale splits a
    # line into a fully-drawn "frozen" batch plus an on-hand batch; together
    # they are still what the purchase put on the shelf.
    by_purchase: dict = {}
    for b in batches:
        if b.purchase_invoice_id is None:
            continue
        key = (b.purchase_invoice_id, b.warehouse_id)
        if key in by_purchase:
            by_purchase[key][1] += b.quantity_initial
        else:
            by_purchase[key] = [b, Decimal(b.quantity_initial)]
    for b, qty in by_purchase.values():
        pi = b.purchase_invoice
        out.append(Movement(
            kind='purchase', date=pi.purchase_date, at=_at(pi.purchase_date, pi.created_at),
            quantity=qty, warehouse=b.warehouse,
            doc_type='purchase_invoice', doc_id=pi.id, doc_number=pi.internal_id,
            counterparty=pi.supplier.name if pi.supplier_id else '',
            note=f'No. faktur vendor {pi.external_invoice_no}' if pi.external_invoice_no else '',
            actor=_name(pi.created_by),
        ))

    # Production — output side. Claim the batch it created so it is not listed
    # a second time as a stock-in.
    runs = (
        ProductionRun.objects.filter(output_item=item)
        .select_related('output_warehouse', 'produced_by', 'recipe')
    )
    for run in runs:
        claimed = None
        if run.produced_batch_id:
            claimed = next((b for b in unlinked if b.id == run.produced_batch_id), None)
            if claimed:
                unlinked.remove(claimed)
        if claimed is None:
            _claim_batch(unlinked, warehouse_id=run.output_warehouse_id,
                         date=_local_date(run.created_at), quantity=run.output_quantity,
                         value=run.total_cost, created_at=run.created_at)
        out.append(Movement(
            kind='production_in', date=_local_date(run.created_at), at=run.created_at,
            quantity=Decimal(run.output_quantity), warehouse=run.output_warehouse,
            doc_type='production_run', doc_id=run.id, doc_number=f'Produksi #{run.id}',
            note=run.recipe.name if run.recipe_id and run.recipe else (run.notes or ''),
            actor=_name(run.produced_by),
        ))

    # Pencacahan — target side, same claim.
    p_in = (
        PencacahanRecord.objects.filter(target_item=item)
        .select_related('target_warehouse', 'source_item', 'created_by')
    )
    for rec in p_in:
        _claim_batch(unlinked, warehouse_id=rec.target_warehouse_id, date=rec.date,
                     quantity=rec.target_quantity, value=rec.value_transferred,
                     created_at=rec.created_at)
        out.append(Movement(
            kind='pencacahan_in', date=rec.date, at=_at(rec.date, rec.created_at),
            quantity=Decimal(rec.target_quantity), warehouse=rec.target_warehouse,
            doc_type='pencacahan', doc_id=rec.id, doc_number=rec.pencacahan_no,
            note=f'Dari [{rec.source_item.code}] {rec.source_item.name}',
            actor=_name(rec.created_by),
        ))

    # Whatever is left was a plain stock-in (or a legacy import).
    for b in unlinked:
        out.append(Movement(
            kind='stock_in', date=b.input_date, at=_at(b.input_date, b.created_at),
            quantity=Decimal(b.quantity_initial), warehouse=b.warehouse,
            doc_type='batch', doc_id=b.id, doc_number=f'Batch #{b.id}',
            actor=_name(b.created_by),
        ))

    # ── Stock out ────────────────────────────────────────────────────────────
    for log in StockOutLog.objects.filter(item=item).select_related('warehouse', 'created_by'):
        note = log.get_reason_display()
        if log.notes:
            note = f'{note} — {log.notes}'
        out.append(Movement(
            kind='stock_out', date=log.out_date, at=_at(log.out_date, log.created_at),
            quantity=-Decimal(log.quantity), warehouse=log.warehouse,
            doc_type='stock_out', doc_id=log.id, doc_number=f'Stok keluar #{log.id}',
            note=note, actor=_name(log.created_by),
        ))

    # ── Stock opname shortages ───────────────────────────────────────────────
    op_items = (
        StockOpnameItem.objects
        .filter(item=item, is_loss=True, session__status='completed')
        .select_related('session', 'warehouse')
    )
    for oi in op_items:
        shortage = oi.system_qty - (oi.shelf1_qty + oi.shelf2_qty)
        if shortage <= 0:
            continue
        s = oi.session
        out.append(Movement(
            kind='opname', date=s.date, at=_at(s.date, s.completed_at),
            quantity=-Decimal(shortage), warehouse=oi.warehouse,
            doc_type='stock_opname', doc_id=s.id, doc_number=f'Opname #{s.id}',
            note=f'Sistem {oi.system_qty}, fisik {oi.shelf1_qty + oi.shelf2_qty}',
            actor=s.conducted_by or '',
        ))

    # ── Production ingredients ───────────────────────────────────────────────
    ings = (
        ProductionRunIngredient.objects.filter(item=item)
        .select_related('run__output_item', 'run__produced_by', 'source_warehouse')
    )
    for ing in ings:
        run = ing.run
        out.append(Movement(
            kind='production_out', date=_local_date(run.created_at), at=run.created_at,
            quantity=-Decimal(ing.quantity_small), warehouse=ing.source_warehouse,
            doc_type='production_run', doc_id=run.id, doc_number=f'Produksi #{run.id}',
            note=(f'Untuk [{run.output_item.code}] {run.output_item.name}'
                  + ('' if ing.source_warehouse_id else ' · diambil FIFO dari semua gudang')),
            actor=_name(run.produced_by),
        ))

    # ── Pencacahan — source side ─────────────────────────────────────────────
    p_out = (
        PencacahanRecord.objects.filter(source_item=item)
        .select_related('source_warehouse', 'target_item', 'created_by')
    )
    for rec in p_out:
        out.append(Movement(
            kind='pencacahan_out', date=rec.date, at=_at(rec.date, rec.created_at),
            quantity=-Decimal(rec.source_quantity), warehouse=rec.source_warehouse,
            doc_type='pencacahan', doc_id=rec.id, doc_number=rec.pencacahan_no,
            note=f'Menjadi [{rec.target_item.code}] {rec.target_item.name}',
            actor=_name(rec.created_by),
        ))

    # A production ingredient drawn "from any warehouse" has no single
    # warehouse, so it cannot be attributed to one under a warehouse filter.
    if warehouse_id is not None:
        out = [m for m in out if _wh(m.warehouse, warehouse_id)]

    out.sort(key=lambda m: m.sort_key)
    balance = ZERO
    for m in out:
        if m.status == APPLIED:
            balance += m.quantity
            m.balance_after = balance
    return out


def serialize(m: Movement) -> dict:
    return {
        'kind': m.kind,
        'kind_label': KIND_LABELS.get(m.kind, m.kind),
        'date': m.date.isoformat(),
        'datetime': m.at.isoformat() if m.at else None,
        'quantity_in': str(m.quantity) if m.quantity > 0 else None,
        'quantity_out': str(-m.quantity) if m.quantity < 0 else None,
        'status': m.status,
        'balance_after': str(m.balance_after) if m.balance_after is not None else None,
        'warehouse': ({'id': m.warehouse.id, 'name': m.warehouse.name}
                      if m.warehouse is not None else None),
        'document': {'type': m.doc_type, 'id': m.doc_id, 'number': m.doc_number},
        'patient': ({'patient_no': m.patient_no, 'name': m.patient_name}
                    if m.patient_no else None),
        'counterparty': m.counterparty,
        'note': m.note,
        'actor': m.actor,
    }
