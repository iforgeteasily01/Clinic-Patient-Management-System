"""GET /api/reports/stock-movement/ and its .xlsx twin.

Classifies every stockable InventoryItem into fast / normal / slow / dead /
never_sold, using the tunable windows on ReportSettings. See
docs/stock-movement-patient-activity-design.md §2 for the exact rules — the
classification order (never_sold -> dead -> slow -> fast -> normal) and the
inclusive tie handling at the fast-window percentile cut are both load-bearing
and reproduced here verbatim, not reinvented.

Movement means a sale (a non-voided InvoiceItem). StockOutLog rows (wastage,
internal issue, treatment consumption) are not movement — an item burned
through internal use is not selling — but the report still surfaces the most
recent stock-out date as a separate column so the operator can tell "dead but
consumed internally" from "dead and untouched".

Performance: this never queries per item. Three aggregate queries (lifetime
last-sale + slow-window quantity, fast-window quantity, stock valuation) plus
one small stock-out aggregate, joined in Python over dicts keyed by item_id —
see module functions below. The catalog is a few thousand rows; that is fine,
one query per item is not.
"""
import datetime
import io
from decimal import Decimal
from zoneinfo import ZoneInfo

import openpyxl
from django.db.models import Max, Q, Sum
from django.http import HttpResponse
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import InventoryItem, InvoiceItem, ReportSettings, StockOutLog
from .inventory_page import stock_valuation_by_item
from .reports_page import _autosize, _write_banner, _write_data_row, _write_table_header

_JAKARTA = ZoneInfo('Asia/Jakarta')

BUCKETS = ('fast', 'normal', 'slow', 'dead', 'never_sold')

_ALLOWED_ORDERINGS = {
    'code', '-code', 'name', '-name',
    'qty_sold', '-qty_sold',
    'last_sold_date', '-last_sold_date',
    'stock_value', '-stock_value',
}

# 'qty_sold'/'-qty_sold' sorts by the fast-window quantity — the response has
# no single "qty_sold" column (it splits fast/slow windows out deliberately),
# and the fast window is the metric a "which items are moving right now"
# report defaults to.
_SORT_FIELD = {
    'code': 'code', 'name': 'name',
    'qty_sold': 'qty_sold_fast_window',
    'last_sold_date': 'last_sold_date',
    'stock_value': 'stock_value',
}


def _local_midnight(d):
    return datetime.datetime(d.year, d.month, d.day, tzinfo=_JAKARTA)


def _parse_as_of(request):
    raw = request.query_params.get('as_of', '').strip()
    if not raw:
        return datetime.date.today(), None
    try:
        return datetime.date.fromisoformat(raw), None
    except ValueError:
        return None, f"Format as_of tidak valid: '{raw}'. Gunakan YYYY-MM-DD."


def _classify(days_since_last_sale, in_fast_window, is_top_percentile, *, slow_days, dead_days):
    """The order in the design doc, reproduced exactly: never_sold -> dead ->
    slow -> fast -> normal. Dead/slow are checked before the ranking branch so
    a stale item can never be pulled back into 'fast' by a coincidental old
    sale — by construction an item in the fast window has days_since <=
    stock_fast_window_days, which ReportSettings validation guarantees is
    shorter than the slow window, so this ordering is never actually
    ambiguous; it is kept explicit anyway because that guarantee lives in a
    different file (api/serializers.py ReportSettingsSerializer).
    """
    if days_since_last_sale is None:
        return 'never_sold'
    if days_since_last_sale >= dead_days:
        return 'dead'
    if days_since_last_sale >= slow_days:
        return 'slow'
    if in_fast_window and is_top_percentile:
        return 'fast'
    return 'normal'


def _build_rows(request):
    """Shared by the JSON view and the .xlsx export.

    Returns (as_of, thresholds_dict, rows, error_response_or_None). ``rows``
    covers every bucket — callers filter by ?bucket themselves, since the
    summary needs the unfiltered set and the JSON `results` page does not.
    """
    as_of, err = _parse_as_of(request)
    if err:
        return None, None, None, Response({'error': err}, status=status.HTTP_400_BAD_REQUEST)

    settings_obj = ReportSettings.get_solo()
    fast_window_days = settings_obj.stock_fast_window_days
    fast_top_percent = settings_obj.stock_fast_top_percent
    slow_days = settings_obj.stock_slow_months * 30
    dead_days = settings_obj.stock_dead_months * 30

    warehouse_id = request.query_params.get('warehouse', '').strip() or None
    category_id = request.query_params.get('category', '').strip() or None
    q = request.query_params.get('q', '').strip()

    items_qs = InventoryItem.objects.filter(is_service=False, is_active=True).select_related('item_category')
    if category_id:
        items_qs = items_qs.filter(item_category_id=category_id)
    if q:
        items_qs = items_qs.filter(Q(code__icontains=q) | Q(name__icontains=q))
    items = list(items_qs)

    as_of_end = _local_midnight(as_of) + datetime.timedelta(days=1)
    fast_start = _local_midnight(as_of - datetime.timedelta(days=fast_window_days - 1))
    slow_start = _local_midnight(as_of - datetime.timedelta(days=slow_days - 1))

    # ── Query 1: lifetime last-sale date + slow-window quantity, per item ──
    sale_lines = InvoiceItem.objects.filter(invoice__is_voided=False, invoice__datetime__lt=as_of_end)
    if warehouse_id:
        sale_lines = sale_lines.filter(invoice__warehouse_id=warehouse_id)

    lifetime = {
        r['item_id']: r
        for r in sale_lines.values('item_id').annotate(
            last_sold=Max('invoice__datetime'),
            qty_slow=Sum('quantity', filter=Q(invoice__datetime__gte=slow_start)),
        )
    }

    # ── Query 2: fast-window quantity, per item ──
    fast = {
        r['item_id']: r['qty_fast']
        for r in sale_lines.filter(invoice__datetime__gte=fast_start)
                            .values('item_id').annotate(qty_fast=Sum('quantity'))
    }

    # ── Query 3: stock on hand + value, per item (shared with StockLevelView) ──
    valuation = stock_valuation_by_item(warehouse_id=warehouse_id)

    # ── Small extra aggregate: most recent internal stock-out, per item ──
    stockout_logs = StockOutLog.objects.all()
    if warehouse_id:
        stockout_logs = stockout_logs.filter(warehouse_id=warehouse_id)
    last_stock_out = {
        r['item_id']: r['last_out']
        for r in stockout_logs.values('item_id').annotate(last_out=Max('out_date'))
    }

    # ── Percentile ranking over items with non-zero sales in the fast window ──
    # Ties at the cut line are resolved inclusively: every item with the same
    # quantity as the last "fast" item is also fast, so two identical items
    # never land in different buckets.
    ranked = sorted(
        ((iid, qty) for iid, qty in fast.items() if qty and qty > 0),
        key=lambda pair: (-pair[1], pair[0]),
    )
    n_ranked = len(ranked)
    cutoff_qty = None
    if n_ranked:
        # Ceiling division: rank 1 of 50 at top_percent=20 -> ranks 1..10.
        cutoff_count = min(max(-(-(n_ranked * fast_top_percent) // 100), 1), n_ranked)
        cutoff_qty = ranked[cutoff_count - 1][1]
    rank_by_item = {iid: idx + 1 for idx, (iid, _qty) in enumerate(ranked)}

    rows = []
    for item in items:
        lt = lifetime.get(item.id)
        last_sold_dt = lt['last_sold'] if lt else None
        last_sold_date = last_sold_dt.astimezone(_JAKARTA).date() if last_sold_dt else None
        days_since = (as_of - last_sold_date).days if last_sold_date else None

        qty_fast = fast.get(item.id) or Decimal('0')
        in_fast_window = item.id in fast and qty_fast > 0
        is_top = bool(in_fast_window and cutoff_qty is not None and qty_fast >= cutoff_qty)

        bucket = _classify(days_since, in_fast_window, is_top, slow_days=slow_days, dead_days=dead_days)

        val = valuation.get(item.id, {})
        rank = rank_by_item.get(item.id)
        rank_percentile = round(rank / n_ranked * 100, 1) if (rank and n_ranked) else None

        qty_slow_raw = lt['qty_slow'] if (lt and lt['qty_slow']) else Decimal('0')

        rows.append({
            'item_id': item.id,
            'code': item.code,
            'name': item.name,
            'category': item.item_category.name if item.item_category_id else '',
            'unit': item.unit_small,
            'qty_on_hand': (val.get('qty_on_hand') or Decimal('0')).quantize(Decimal('0.001')),
            'stock_value': (val.get('stock_value') or Decimal('0')).quantize(Decimal('0.01')),
            'qty_sold_fast_window': qty_fast.quantize(Decimal('0.001')),
            'qty_sold_slow_window': qty_slow_raw.quantize(Decimal('0.001')),
            'last_sold_date': last_sold_date,
            'days_since_last_sale': days_since,
            'last_stock_out_date': last_stock_out.get(item.id),
            'bucket': bucket,
            'rank_in_window': rank,
            'rank_percentile': rank_percentile,
        })

    thresholds = {
        'stock_fast_window_days': fast_window_days,
        'stock_fast_top_percent': fast_top_percent,
        'stock_slow_months': settings_obj.stock_slow_months,
        'stock_dead_months': settings_obj.stock_dead_months,
    }
    return as_of, thresholds, rows, None


def _sort_rows(rows, ordering):
    reverse = ordering.startswith('-')
    field = _SORT_FIELD[ordering.lstrip('-')]
    non_null = [r for r in rows if r[field] is not None]
    null_rows = [r for r in rows if r[field] is None]
    non_null.sort(key=lambda r: r[field], reverse=reverse)
    return non_null + null_rows


def _serialize_row(r):
    return {
        'item_id': r['item_id'], 'code': r['code'], 'name': r['name'],
        'category': r['category'], 'unit': r['unit'],
        'qty_on_hand': str(r['qty_on_hand']), 'stock_value': str(r['stock_value']),
        'qty_sold_fast_window': str(r['qty_sold_fast_window']),
        'qty_sold_slow_window': str(r['qty_sold_slow_window']),
        'last_sold_date': str(r['last_sold_date']) if r['last_sold_date'] else None,
        'days_since_last_sale': r['days_since_last_sale'],
        'last_stock_out_date': str(r['last_stock_out_date']) if r['last_stock_out_date'] else None,
        'bucket': r['bucket'],
        'rank_in_window': r['rank_in_window'],
        'rank_percentile': r['rank_percentile'],
    }


def _paginate(request, rows):
    try:
        page = max(1, int(request.query_params.get('page', 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.query_params.get('page_size', 50))
    except (TypeError, ValueError):
        page_size = 50
    page_size = min(max(page_size, 1), 200)

    count = len(rows)
    num_pages = max(1, -(-count // page_size))
    start = (page - 1) * page_size
    return rows[start:start + page_size], count, page, page_size, num_pages


class StockMovementReportView(APIView):
    def get(self, request):
        as_of, thresholds, rows, err = _build_rows(request)
        if err:
            return err

        summary = {b: {'count': 0, 'qty_on_hand': Decimal('0'), 'stock_value': Decimal('0')} for b in BUCKETS}
        for r in rows:
            s = summary[r['bucket']]
            s['count'] += 1
            s['qty_on_hand'] += r['qty_on_hand']
            s['stock_value'] += r['stock_value']

        bucket_param = request.query_params.get('bucket', '').strip().lower()
        if bucket_param:
            if bucket_param not in BUCKETS:
                return Response(
                    {'error': f"Bucket tidak dikenal: '{bucket_param}'. Pilihan: {', '.join(BUCKETS)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            filtered = [r for r in rows if r['bucket'] == bucket_param]
        else:
            filtered = rows

        ordering = request.query_params.get('ordering', '').strip()
        if ordering not in _ALLOWED_ORDERINGS:
            ordering = '-qty_sold'
        filtered = _sort_rows(filtered, ordering)

        page_rows, count, page, page_size, num_pages = _paginate(request, filtered)

        return Response({
            'as_of': str(as_of),
            'thresholds': thresholds,
            'summary': {
                b: {
                    'count': s['count'],
                    'qty_on_hand': str(s['qty_on_hand'].quantize(Decimal('0.001'))),
                    'stock_value': str(s['stock_value'].quantize(Decimal('0.01'))),
                }
                for b, s in summary.items()
            },
            'count': count, 'page': page, 'page_size': page_size, 'num_pages': num_pages,
            'results': [_serialize_row(r) for r in page_rows],
        })


# ── .xlsx export ─────────────────────────────────────────────────────────────

_EXPORT_COLUMNS = [
    'Kode', 'Nama', 'Kategori', 'Satuan', 'Stok', 'Nilai Stok',
    'Terjual (Fast Window)', 'Terjual (Slow Window)',
    'Tgl Jual Terakhir', 'Hari Sejak Jual', 'Tgl Keluar Terakhir', 'Peringkat',
]

_BUCKET_LABELS = {
    'fast': 'Cepat (Fast)',
    'normal': 'Normal',
    'slow': 'Lambat (Slow)',
    'dead': 'Mati (Dead)',
    'never_sold': 'Belum Pernah Terjual',
}

_SUMMARY_COLUMNS = ['Kategori', 'Jumlah Item', 'Nilai Stok']


def _row_to_excel(r):
    return [
        r['code'], r['name'], r['category'], r['unit'],
        float(r['qty_on_hand']), float(r['stock_value']),
        float(r['qty_sold_fast_window']), float(r['qty_sold_slow_window']),
        r['last_sold_date'].isoformat() if r['last_sold_date'] else '',
        r['days_since_last_sale'] if r['days_since_last_sale'] is not None else '',
        r['last_stock_out_date'].isoformat() if r['last_stock_out_date'] else '',
        r['rank_in_window'] if r['rank_in_window'] is not None else '',
    ]


def _build_workbook(as_of, rows, ordering):
    """One Summary sheet + one sheet per bucket, built from the same
    ``_write_banner``/``_write_table_header``/``_write_data_row``/``_autosize``
    primitives ``reports_page._report_to_xlsx`` uses. That function itself
    isn't reusable verbatim here — it builds exactly one sheet — so this
    mirrors its finalization pattern instead of calling it.
    """
    wb = openpyxl.Workbook()
    period_label = f'per {as_of.isoformat()}'

    ws = wb.active
    ws.title = 'Ringkasan'
    ws.sheet_view.showGridLines = False
    row_i, _has_logo = _write_banner(ws, 'Laporan Pergerakan Stok', period_label, len(_SUMMARY_COLUMNS))
    row_i = _write_table_header(ws, row_i, _SUMMARY_COLUMNS)
    summary_rows = []
    for b in BUCKETS:
        bucket_rows = [row for row in rows if row['bucket'] == b]
        value = sum((row['stock_value'] for row in bucket_rows), Decimal('0'))
        data_row = [_BUCKET_LABELS[b], len(bucket_rows), float(value)]
        summary_rows.append(data_row)
        row_i = _write_data_row(ws, row_i, data_row)
    _autosize(ws, _SUMMARY_COLUMNS, summary_rows)

    for b in BUCKETS:
        bucket_rows = _sort_rows([row for row in rows if row['bucket'] == b], ordering)
        sheet = wb.create_sheet(_BUCKET_LABELS[b][:31])
        sheet.sheet_view.showGridLines = False
        row_i, _has_logo = _write_banner(
            sheet, f'Pergerakan Stok – {_BUCKET_LABELS[b]}', period_label, len(_EXPORT_COLUMNS),
        )
        row_i = _write_table_header(sheet, row_i, _EXPORT_COLUMNS)
        excel_rows = [_row_to_excel(row) for row in bucket_rows]
        for data_row in excel_rows:
            row_i = _write_data_row(sheet, row_i, data_row)
        _autosize(sheet, _EXPORT_COLUMNS, excel_rows)

    return wb


class StockMovementExportView(APIView):
    """GET /api/reports/stock-movement/export/ — same params, returns .xlsx."""

    def get(self, request):
        as_of, _thresholds, rows, err = _build_rows(request)
        if err:
            return err

        ordering = request.query_params.get('ordering', '').strip()
        if ordering not in _ALLOWED_ORDERINGS:
            ordering = '-qty_sold'

        wb = _build_workbook(as_of, rows, ordering)
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f'stock_movement_{as_of}.xlsx'
        response = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
