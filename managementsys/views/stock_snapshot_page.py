"""Stock snapshots — the master item list with quantity and value on a given day.

All the capture and comparison logic is in ``services/stock_snapshot.py``; this
file parses parameters, shapes JSON and builds the workbook.

| Method | Path | |
|---|---|---|
| GET  | /api/inventory/snapshots/              | Headers, newest first (``date_from``/``date_to``) |
| POST | /api/inventory/snapshots/              | Take one now (``{notes}``) |
| GET  | /api/inventory/snapshots/<pk>/         | Header + item rows (``warehouse_id``, ``q``, ``compare_to``) |
| DELETE | /api/inventory/snapshots/<pk>/       | Superuser only |
| GET  | /api/inventory/snapshots/<pk>/export/  | ``.xlsx``, same parameters as detail |
"""
import datetime
import io
from decimal import Decimal

import openpyxl
from openpyxl.styles import Font
from django.http import HttpResponse
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import JAKARTA_TZ, AuditLog, StockSnapshot
from ..services.stock_snapshot import compare, item_rows, reconciliation, take_snapshot
from .reports_page import _autosize, _write_banner, _write_data_row, _write_table_header

# Stock value is the clinic's balance sheet, so the same two roles that read the
# kartu stok and the financial reports.
READER_ROLES = ('superuser', 'manager')
DELETE_ROLES = ('superuser',)
LIST_LIMIT = 500

MONEY_FMT = '#,##0;[Red]-#,##0'
QTY_FMT = '#,##0.####;[Red]-#,##0.####'


def _role(request):
    return getattr(request.user, 'role', None)


def _forbidden():
    return Response({'error': 'Tidak diizinkan.'}, status=403)


def _s(d):
    return None if d is None else str(d)


def _header(snap: StockSnapshot) -> dict:
    rec = reconciliation(snap)
    return {
        'id': snap.id,
        'snapshot_date': snap.snapshot_date.isoformat(),
        'taken_at': snap.taken_at.isoformat(),
        'taken_by': snap.taken_by.display_name if snap.taken_by_id else None,
        'source': snap.source,
        'notes': snap.notes,
        'item_count': snap.item_count,
        'items_in_stock': snap.items_in_stock,
        'total_quantity': str(snap.total_quantity),
        'total_value': str(snap.total_value),
        'unposted_purchase_value': str(snap.unposted_purchase_value),
        'unposted_stock_out_value': str(snap.unposted_stock_out_value),
        'gl_balance': _s(rec['gl_balance']),
        'difference': _s(rec['difference']),
        'expected_gl': _s(rec['expected_gl']),
        'unexplained_difference': _s(rec['unexplained_difference']),
    }


def _parse_date(raw):
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return False


def _load(pk):
    return StockSnapshot.objects.select_related('taken_by').filter(pk=pk).first()


def _rows_for(request, snap):
    """(rows, warehouse_id, base_snapshot, error_response)."""
    q = request.query_params
    warehouse_id = None
    if q.get('warehouse_id'):
        try:
            warehouse_id = int(q['warehouse_id'])
        except ValueError:
            return None, None, None, Response({'error': 'warehouse_id tidak valid.'}, status=400)
    search = (q.get('q') or '').strip()
    rows = item_rows(snap, warehouse_id=warehouse_id, q=search)

    base = None
    if q.get('compare_to'):
        try:
            base = _load(int(q['compare_to']))
        except ValueError:
            base = None
        if base is None:
            return None, None, None, Response({'error': 'Snapshot pembanding tidak ditemukan.'},
                                              status=400)
        rows = compare(rows, item_rows(base, warehouse_id=warehouse_id, q=search))
    if q.get('in_stock') == 'true':
        rows = [r for r in rows if r['quantity'] or r.get('base_quantity')]
    return rows, warehouse_id, base, None


def _warehouses(snap):
    seen = {}
    for wid, name in (snap.lines.exclude(warehouse_id=None)
                      .values_list('warehouse_id', 'warehouse_name').distinct()):
        seen[wid] = name
    return [{'id': k, 'name': v} for k, v in sorted(seen.items(), key=lambda kv: kv[1])]


def _row_json(r):
    out = {
        'item_id': r['item_id'], 'code': r['code'], 'name': r['name'],
        'category': r['category'], 'unit': r['unit'],
        'selling_price': str(r['selling_price']), 'min_stock': r['min_stock'],
        'is_active': r['is_active'],
        'quantity': str(r['quantity']), 'value': str(r['value']),
        'unit_cost': _s(r['unit_cost']),
        'warehouses': [{'id': w['id'], 'name': w['name'],
                        'quantity': str(w['quantity']), 'value': str(w['value'])}
                       for w in r['warehouses']],
    }
    if 'base_quantity' in r:
        out.update({
            'base_quantity': str(r['base_quantity']), 'base_value': str(r['base_value']),
            'delta_quantity': str(r['delta_quantity']), 'delta_value': str(r['delta_value']),
            'missing_now': bool(r.get('missing_now')),
        })
    return out


class StockSnapshotListView(APIView):
    def get(self, request):
        if _role(request) not in READER_ROLES:
            return _forbidden()
        date_from = _parse_date(request.query_params.get('date_from'))
        date_to = _parse_date(request.query_params.get('date_to'))
        if date_from is False or date_to is False:
            return Response({'error': 'Format tanggal harus YYYY-MM-DD.'}, status=400)
        qs = StockSnapshot.objects.select_related('taken_by')
        if date_from:
            qs = qs.filter(snapshot_date__gte=date_from)
        if date_to:
            qs = qs.filter(snapshot_date__lte=date_to)
        return Response({'results': [_header(s) for s in qs[:LIST_LIMIT]]})

    def post(self, request):
        if _role(request) not in READER_ROLES:
            return _forbidden()
        notes = str(request.data.get('notes') or '').strip()[:1000]
        snap = take_snapshot(user=request.user, notes=notes)
        AuditLog.objects.create(
            performed_by=request.user, action='CREATE', entity_type='StockSnapshot',
            entity_id=str(snap.id),
            description=(f'Snapshot stok {snap.snapshot_date}: {snap.items_in_stock} item berstok, '
                         f'nilai Rp {snap.total_value:,.0f}'),
        )
        return Response(_header(snap), status=201)


class StockSnapshotDetailView(APIView):
    def get(self, request, pk):
        if _role(request) not in READER_ROLES:
            return _forbidden()
        snap = _load(pk)
        if snap is None:
            return Response({'error': 'Not found.'}, status=404)
        rows, warehouse_id, base, err = _rows_for(request, snap)
        if err:
            return err
        return Response({
            'snapshot': _header(snap),
            'compare_to': _header(base) if base else None,
            'warehouse_id': warehouse_id,
            'warehouses': _warehouses(snap),
            'totals': {
                'quantity': str(sum((r['quantity'] for r in rows), Decimal('0'))),
                'value': str(sum((r['value'] for r in rows), Decimal('0'))),
                'rows': len(rows),
            },
            'results': [_row_json(r) for r in rows],
        })

    def delete(self, request, pk):
        if _role(request) not in DELETE_ROLES:
            return _forbidden()
        snap = _load(pk)
        if snap is None:
            return Response({'error': 'Not found.'}, status=404)
        AuditLog.objects.create(
            performed_by=request.user, action='DELETE', entity_type='StockSnapshot',
            entity_id=str(snap.id),
            description=f'Hapus snapshot stok {snap.snapshot_date} (#{snap.id}), '
                        f'nilai Rp {snap.total_value:,.0f}',
        )
        snap.delete()
        return Response(status=204)


# ── Excel ──────────────────────────────────────────────────────────────────

def _stamp(snap):
    return snap.taken_at.astimezone(JAKARTA_TZ).strftime('%d-%m-%Y %H:%M WIB')


def _fmt_cols(ws, first_row, last_row, cols, fmt):
    for c in cols:
        for r in range(first_row, last_row):
            ws.cell(r, c).number_format = fmt


def _build_workbook(snap, rows, base, warehouse_name):
    wb = openpyxl.Workbook()
    scope = f' · Gudang {warehouse_name}' if warehouse_name else ''
    label = f'per {_stamp(snap)}{scope}'

    # Ringkasan
    ws = wb.active
    ws.title = 'Ringkasan'
    ws.sheet_view.showGridLines = False
    cols = ['Keterangan', 'Nilai']
    r, _ = _write_banner(ws, 'Snapshot Stok', label, len(cols))
    r = _write_table_header(ws, r, cols)
    rec = reconciliation(snap)
    summary = [
        ['Diambil oleh', snap.taken_by.display_name if snap.taken_by_id else 'Otomatis'],
        ['Jumlah item (master)', snap.item_count],
        ['Item dengan stok', snap.items_in_stock],
        ['Nilai stok (batch)', float(snap.total_value)],
        ['Saldo GL Persediaan (1300000)',
         float(rec['gl_balance']) if rec['gl_balance'] is not None else 'Tidak ada'],
        ['Selisih batch − GL', float(rec['difference']) if rec['difference'] is not None else ''],
        ['+ Pembelian belum dijurnal', float(snap.unposted_purchase_value)],
        ['− Stok keluar belum dijurnal', float(snap.unposted_stock_out_value)],
        ['Selisih tak terjelaskan',
         float(rec['unexplained_difference']) if rec['unexplained_difference'] is not None else ''],
    ]
    if snap.notes:
        summary.append(['Catatan', snap.notes])
    if base:
        summary += [
            ['Dibandingkan dengan', f'Snapshot {_stamp(base)}'],
            ['Nilai stok pembanding', float(base.total_value)],
            ['Perubahan nilai', float(snap.total_value - base.total_value)],
        ]
    start = r
    for row in summary:
        r = _write_data_row(ws, r, row)
    _fmt_cols(ws, start, r, [2], MONEY_FMT)
    _autosize(ws, cols, summary)

    # Per Item — the master list
    ws = wb.create_sheet('Per Item')
    ws.sheet_view.showGridLines = False
    cols = ['Kode', 'Nama', 'Kategori', 'Satuan', 'Harga Jual', 'Min Stok', 'Aktif',
            'Qty', 'Nilai', 'HPP Rata-rata']
    if base:
        cols += ['Qty Pembanding', 'Nilai Pembanding', 'Δ Qty', 'Δ Nilai']
    r, _ = _write_banner(ws, 'Snapshot Stok – Master Item', label, len(cols))
    r = _write_table_header(ws, r, cols)
    start = r
    data = []
    for it in rows:
        row = [it['code'], it['name'], it['category'], it['unit'],
               float(it['selling_price']), it['min_stock'], 'Ya' if it['is_active'] else 'Tidak',
               float(it['quantity']), float(it['value']),
               float(it['unit_cost']) if it['unit_cost'] is not None else '']
        if base:
            row += [float(it['base_quantity']), float(it['base_value']),
                    float(it['delta_quantity']), float(it['delta_value'])]
        data.append(row)
        r = _write_data_row(ws, r, row)
    total = ['TOTAL', '', '', '', '', '', '',
             float(sum((it['quantity'] for it in rows), Decimal('0'))),
             float(sum((it['value'] for it in rows), Decimal('0'))), '']
    if base:
        total += [float(sum((it['base_quantity'] for it in rows), Decimal('0'))),
                  float(sum((it['base_value'] for it in rows), Decimal('0'))),
                  float(sum((it['delta_quantity'] for it in rows), Decimal('0'))),
                  float(sum((it['delta_value'] for it in rows), Decimal('0')))]
    r = _write_data_row(ws, r, total)
    for c in range(1, len(cols) + 1):
        ws.cell(r - 1, c).font = Font(bold=True)
    _fmt_cols(ws, start, r, [5, 9, 10] + ([12, 14] if base else []), MONEY_FMT)
    _fmt_cols(ws, start, r, [8] + ([11, 13] if base else []), QTY_FMT)
    ws.freeze_panes = ws.cell(start, 3)
    _autosize(ws, cols, data)

    # Per Gudang — one row per item per warehouse with stock
    ws = wb.create_sheet('Per Gudang')
    ws.sheet_view.showGridLines = False
    cols = ['Gudang', 'Kode', 'Nama', 'Satuan', 'Qty', 'Nilai']
    r, _ = _write_banner(ws, 'Snapshot Stok – Per Gudang', label, len(cols))
    r = _write_table_header(ws, r, cols)
    start = r
    data = []
    for it in sorted(rows, key=lambda x: x['code']):
        for w in it['warehouses']:
            data.append([w['name'], it['code'], it['name'], it['unit'],
                         float(w['quantity']), float(w['value'])])
    data.sort(key=lambda x: (x[0], x[1]))
    for row in data:
        r = _write_data_row(ws, r, row)
    _fmt_cols(ws, start, r, [6], MONEY_FMT)
    _fmt_cols(ws, start, r, [5], QTY_FMT)
    ws.freeze_panes = ws.cell(start, 1)
    _autosize(ws, cols, data)
    return wb


class StockSnapshotExportView(APIView):
    def get(self, request, pk):
        if _role(request) not in READER_ROLES:
            return _forbidden()
        snap = _load(pk)
        if snap is None:
            return Response({'error': 'Not found.'}, status=404)
        rows, warehouse_id, base, err = _rows_for(request, snap)
        if err:
            return err
        wh_name = next((w['name'] for w in _warehouses(snap) if w['id'] == warehouse_id), None)
        wb = _build_workbook(snap, rows, base, wh_name)
        buf = io.BytesIO()
        wb.save(buf)
        stamp = snap.taken_at.astimezone(JAKARTA_TZ).strftime('%Y%m%d_%H%M')
        response = HttpResponse(
            buf.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="snapshot_stok_{stamp}.xlsx"'
        return response
