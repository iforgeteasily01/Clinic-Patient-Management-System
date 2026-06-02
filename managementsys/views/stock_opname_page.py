from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.db.models.functions import Coalesce
from django.db.models import Value
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import (
    AppUser, AuditLog,
    InventoryBatch, InventoryItem, Warehouse,
    StockOpnameSession, StockOpnameItem,
)
from .inventory_page import _fifo_deduct


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _serialize_session(session, include_items=False):
    data = {
        'id': session.id,
        'date': str(session.date),
        'conducted_by': session.conducted_by,
        'notes': session.notes,
        'status': session.status,
        'started_at': session.started_at.isoformat() if session.started_at else None,
        'completed_at': session.completed_at.isoformat() if session.completed_at else None,
        'created_at': session.created_at.isoformat(),
        'created_by_name': session.created_by.full_name if session.created_by else None,
    }
    if include_items:
        items = session.items.select_related('item', 'warehouse').all()
        data['warehouse_id'] = items[0].warehouse_id if items else None
        data['warehouse_name'] = items[0].warehouse.name if items else None
        data['items'] = [
            {
                'item_id': it.item_id,
                'item_code': it.item.code,
                'item_name': it.item.name,
                'item_unit_small': it.item.unit_small,
                'warehouse_id': it.warehouse_id,
                'warehouse_name': it.warehouse.name,
                'shelf1_qty': it.shelf1_qty,
                'shelf2_qty': it.shelf2_qty,
                'system_qty': it.system_qty,
                'is_loss': it.is_loss,
            }
            for it in items
        ]
    else:
        first = session.items.select_related('warehouse').first()
        data['warehouse_id'] = first.warehouse_id if first else None
        data['warehouse_name'] = first.warehouse.name if first else None
        data['item_count'] = session.items.count()
    return data


def _save_items(session, warehouse_id, items_data):
    for row in items_data:
        StockOpnameItem.objects.update_or_create(
            session=session,
            item_id=int(row['item_id']),
            warehouse_id=int(warehouse_id),
            defaults={
                'shelf1_qty': int(row.get('shelf1_qty') or 0),
                'shelf2_qty': int(row.get('shelf2_qty') or 0),
                'system_qty': int(row.get('system_qty') or 0),
                'is_loss': bool(row.get('is_loss', False)),
            },
        )


class StockOpnameSessionListCreateView(APIView):
    def get(self, request):
        sessions = StockOpnameSession.objects.all()
        return Response([_serialize_session(s) for s in sessions])

    def post(self, request):
        data = request.data
        date = data.get('date')
        conducted_by = (data.get('conducted_by') or '').strip()
        notes = (data.get('notes') or '').strip()
        warehouse_id = data.get('warehouse_id')
        items_data = data.get('items', [])

        if not date or not conducted_by or not warehouse_id:
            return Response(
                {'error': 'Tanggal, nama petugas, dan gudang wajib diisi.'},
                status=400,
            )

        with transaction.atomic():
            session = StockOpnameSession.objects.create(
                date=date,
                conducted_by=conducted_by,
                notes=notes,
                status='draft',
                started_at=timezone.now(),
                created_by=_actor(request),
            )
            _save_items(session, warehouse_id, items_data)

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='StockOpnameSession',
            entity_id=str(session.id),
            description=(
                f'Sesi stok opname #{session.id} dimulai oleh {conducted_by} '
                f'untuk tanggal {date}.'
            ),
        )
        return Response(_serialize_session(session, include_items=True), status=201)


class StockOpnameSessionDetailView(APIView):
    def _get(self, pk):
        try:
            return StockOpnameSession.objects.get(pk=pk)
        except StockOpnameSession.DoesNotExist:
            return None

    def get(self, request, pk):
        session = self._get(pk)
        if session is None:
            return Response({'error': 'Tidak ditemukan.'}, status=404)
        return Response(_serialize_session(session, include_items=True))

    def put(self, request, pk):
        session = self._get(pk)
        if session is None:
            return Response({'error': 'Tidak ditemukan.'}, status=404)
        if session.status == 'completed':
            return Response({'error': 'Sesi yang sudah selesai tidak dapat diubah.'}, status=400)

        data = request.data
        warehouse_id = data.get('warehouse_id') or (
            session.items.values_list('warehouse_id', flat=True).first()
        )
        items_data = data.get('items', [])

        if 'conducted_by' in data and data['conducted_by']:
            session.conducted_by = data['conducted_by'].strip()
        if 'notes' in data:
            session.notes = (data['notes'] or '').strip()
        if 'date' in data and data['date']:
            session.date = data['date']
        session.save()

        with transaction.atomic():
            session.items.all().delete()
            _save_items(session, warehouse_id, items_data)

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='StockOpnameSession',
            entity_id=str(session.id),
            description=f'Draft stok opname #{session.id} diperbarui.',
        )
        return Response(_serialize_session(session, include_items=True))


class StockOpnameCompleteView(APIView):
    def post(self, request, pk):
        try:
            session = StockOpnameSession.objects.get(pk=pk)
        except StockOpnameSession.DoesNotExist:
            return Response({'error': 'Tidak ditemukan.'}, status=404)

        if session.status == 'completed':
            return Response({'error': 'Sesi sudah diselesaikan.'}, status=400)

        data = request.data
        warehouse_id = data.get('warehouse_id') or (
            session.items.values_list('warehouse_id', flat=True).first()
        )
        items_data = data.get('items', [])

        with transaction.atomic():
            session.items.all().delete()
            _save_items(session, warehouse_id, items_data)

            total_loss_qty = 0
            total_loss_cogs = Decimal('0')

            for row in items_data:
                if not row.get('is_loss'):
                    continue
                item_id = int(row['item_id'])
                physical_total = int(row.get('shelf1_qty') or 0) + int(row.get('shelf2_qty') or 0)
                system_qty = int(row.get('system_qty') or 0)
                shortage = system_qty - physical_total
                if shortage <= 0:
                    continue

                shortfall, cogs = _fifo_deduct(item_id, int(warehouse_id), shortage)
                total_loss_qty += shortage - shortfall
                total_loss_cogs += cogs

                AuditLog.objects.create(
                    performed_by=_actor(request),
                    action='UPDATE',
                    entity_type='StockOpnameLoss',
                    entity_id=str(session.id),
                    description=(
                        f'Stok opname #{session.id}: item #{item_id} '
                        f'dicatat susut {shortage} {row.get("item_unit_small", "unit")} '
                        f'(HPP Rp {cogs:,.0f}).'
                    ),
                )

            session.status = 'completed'
            session.completed_at = timezone.now()
            session.save()

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='StockOpnameSession',
            entity_id=str(session.id),
            description=(
                f'Stok opname #{session.id} oleh {session.conducted_by} '
                f'pada {session.date} diselesaikan. '
                f'Total susut: {total_loss_qty} unit (HPP Rp {total_loss_cogs:,.0f}).'
            ),
        )
        return Response(_serialize_session(session, include_items=True))
