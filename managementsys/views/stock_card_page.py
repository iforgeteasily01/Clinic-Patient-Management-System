"""GET /api/inventory/items/<pk>/movements/ — the item's kartu stok.

All the work is in ``services/stock_card.py``; this view filters, totals and
pages what it returns. The running balance is always computed over the item's
*whole* history first and only then cut to the requested window, so a row's
``balance_after`` is the same whatever date range the reader picked.
"""
import datetime
from decimal import Decimal

from django.db.models import Sum
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import InventoryBatch, InventoryItem
from ..services.stock_card import APPLIED, KIND_LABELS, PENDING, item_movements, serialize

# Rows name patients, so this is not open to every role that can log in.
READER_ROLES = ('superuser', 'manager')
MAX_PAGE_SIZE = 500


def _parse_date(raw):
    if not raw:
        return None
    try:
        return datetime.date.fromisoformat(raw)
    except ValueError:
        return False


class InventoryItemMovementsView(APIView):
    def get(self, request, pk):
        if getattr(request.user, 'role', None) not in READER_ROLES:
            return Response({'error': 'Tidak diizinkan.'}, status=403)
        try:
            item = InventoryItem.objects.get(pk=pk)
        except InventoryItem.DoesNotExist:
            return Response({'error': 'Not found.'}, status=404)

        q = request.query_params
        warehouse_id = None
        if q.get('warehouse_id'):
            try:
                warehouse_id = int(q['warehouse_id'])
            except ValueError:
                return Response({'error': 'warehouse_id tidak valid.'}, status=400)
        date_from = _parse_date(q.get('date_from'))
        date_to = _parse_date(q.get('date_to'))
        if date_from is False or date_to is False:
            return Response({'error': 'Format tanggal harus YYYY-MM-DD.'}, status=400)
        kinds = {k for k in (q.get('kind') or '').split(',') if k}
        unknown = kinds - set(KIND_LABELS)
        if unknown:
            return Response({'error': f'Jenis tidak dikenal: {", ".join(sorted(unknown))}'},
                            status=400)
        try:
            page = max(1, int(q.get('page') or 1))
            page_size = min(MAX_PAGE_SIZE, max(1, int(q.get('page_size') or 100)))
        except ValueError:
            return Response({'error': 'page/page_size tidak valid.'}, status=400)

        batches = InventoryBatch.objects.filter(item=item)
        if warehouse_id is not None:
            batches = batches.filter(warehouse_id=warehouse_id)
        on_hand = batches.aggregate(t=Sum('quantity_remaining'))['t'] or Decimal('0')

        movements = [] if item.is_service else item_movements(item, warehouse_id=warehouse_id)
        history_balance = next(
            (m.balance_after for m in reversed(movements) if m.balance_after is not None),
            Decimal('0'),
        )
        opening = Decimal('0')
        if date_from:
            opening = sum((m.quantity for m in movements
                           if m.status == APPLIED and m.date < date_from), Decimal('0'))

        window = [
            m for m in movements
            if (not date_from or m.date >= date_from)
            and (not date_to or m.date <= date_to)
            and (not kinds or m.kind in kinds)
        ]

        def total(pred):
            return str(sum((abs(m.quantity) for m in window if pred(m)), Decimal('0')))

        totals = {
            'in': total(lambda m: m.status == APPLIED and m.quantity > 0),
            'out': total(lambda m: m.status == APPLIED and m.quantity < 0),
            'pending_in': total(lambda m: m.status == PENDING and m.quantity > 0),
            'pending_out': total(lambda m: m.status == PENDING and m.quantity < 0),
            'pending_count': sum(1 for m in window if m.status == PENDING),
        }

        window.reverse()   # newest first for reading
        count = len(window)
        num_pages = max(1, -(-count // page_size))
        start = (page - 1) * page_size

        return Response({
            'item': {
                'id': item.id, 'code': item.code, 'name': item.name,
                'unit_small': item.unit_small, 'is_service': item.is_service,
            },
            'warehouse_id': warehouse_id,
            'date_from': date_from.isoformat() if date_from else None,
            'date_to': date_to.isoformat() if date_to else None,
            'on_hand': str(on_hand),
            'history_balance': str(history_balance),
            'discrepancy': str(on_hand - history_balance),
            'opening_balance': str(opening),
            'totals': totals,
            'kinds': KIND_LABELS,
            'count': count,
            'page': page,
            'page_size': page_size,
            'num_pages': num_pages,
            'results': [serialize(m) for m in window[start:start + page_size]],
        })
