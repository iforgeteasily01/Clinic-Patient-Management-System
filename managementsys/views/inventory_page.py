import io
from decimal import Decimal

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
from django.db import transaction
from django.db import models
from django.db.models import Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

_HEADER_FONT = Font(bold=True, color='FFFFFF')
_HEADER_FILL = PatternFill('solid', fgColor='0284C7')

from ..api.serializers import (
    InventoryBatchSerializer,
    InventoryItemSerializer,
    ItemSyncSerializer,
    WarehouseSerializer,
)
from ..models import AppUser, AuditLog, InventoryBatch, InventoryItem, Warehouse


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


# ── Items ──────────────────────────────────────────────────────────────────

class InventoryItemListCreateView(APIView):
    def get(self, request):
        qs = InventoryItem.objects.annotate(
            total_stock=Coalesce(Sum('batches__quantity_remaining'), Value(0))
        )
        # Inventory admin pages pass stock_only=1 to hide service mirror items.
        # The POS omits this flag so package mirror items are scannable by code.
        if request.GET.get('stock_only') == '1':
            qs = qs.filter(is_service=False)
        if request.GET.get('active_only') == '1':
            qs = qs.filter(is_active=True)
        if search := request.GET.get('search', '').strip():
            qs = qs.filter(
                models.Q(code__icontains=search) | models.Q(name__icontains=search)
            )
        return Response(InventoryItemSerializer(qs, many=True).data)

    def post(self, request):
        serializer = InventoryItemSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        instance = serializer.save(created_by=_actor(request))
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='InventoryItem',
            entity_id=str(instance.id),
            description=f'Inventory item created: [{instance.code}] {instance.name}',
        )
        return Response(InventoryItemSerializer(instance).data, status=status.HTTP_201_CREATED)


class InventoryItemDetailView(APIView):
    def _get(self, pk):
        try:
            return InventoryItem.objects.get(pk=pk)
        except InventoryItem.DoesNotExist:
            return None

    def get(self, request, pk):
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(InventoryItemSerializer(instance).data)

    def put(self, request, pk):
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if instance.is_service:
            return Response({'error': 'Service items are managed through Treatments.'}, status=status.HTTP_400_BAD_REQUEST)
        serializer = InventoryItemSerializer(instance, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='InventoryItem',
            entity_id=str(pk),
            description=f'Inventory item updated: [{instance.code}] {instance.name}',
        )
        return Response(serializer.data)

    def delete(self, request, pk):
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if instance.is_service:
            return Response({'error': 'Service items are managed through Treatments.'}, status=status.HTTP_400_BAD_REQUEST)
        instance.is_active = False
        instance.save(update_fields=['is_active'])
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='InventoryItem',
            entity_id=str(pk),
            description=f'Inventory item deactivated: [{instance.code}] {instance.name}',
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Warehouses ─────────────────────────────────────────────────────────────

class WarehouseListCreateView(APIView):
    def get(self, request):
        return Response(WarehouseSerializer(Warehouse.objects.all(), many=True).data)

    def post(self, request):
        serializer = WarehouseSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        instance = serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='Warehouse',
            entity_id=str(instance.id),
            description=f'Warehouse created: [{instance.code}] {instance.name}',
        )
        return Response(WarehouseSerializer(instance).data, status=status.HTTP_201_CREATED)


class WarehouseDetailView(APIView):
    def _get(self, pk):
        try:
            return Warehouse.objects.get(pk=pk)
        except Warehouse.DoesNotExist:
            return None

    def put(self, request, pk):
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        serializer = WarehouseSerializer(instance, data=request.data, partial=True)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        serializer.save()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='Warehouse',
            entity_id=str(pk),
            description=f'Warehouse updated: {instance.name}',
        )
        return Response(serializer.data)

    def delete(self, request, pk):
        instance = self._get(pk)
        if instance is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if InventoryBatch.objects.filter(warehouse_id=pk).exists():
            return Response(
                {'error': 'Cannot delete warehouse with existing inventory records.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='Warehouse',
            entity_id=str(pk),
            description=f'Warehouse deleted: {instance.name}',
        )
        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Stock levels ───────────────────────────────────────────────────────────

class StockLevelView(APIView):
    def get(self, request):
        qs = (
            InventoryBatch.objects
            .values(
                'item_id', 'warehouse_id',
                'item__code', 'item__name', 'item__unit_small',
                'item__unit_medium', 'item__unit_medium_qty',
                'item__unit_large', 'item__unit_large_qty',
                'item__min_stock', 'item__selling_price', 'item__is_active',
                'warehouse__code', 'warehouse__name',
            )
            .annotate(total_quantity=Sum('quantity_remaining'))
            .order_by('item__name', 'warehouse__name')
        )
        result = [
            {
                'item_id': r['item_id'],
                'item_code': r['item__code'],
                'item_name': r['item__name'],
                'unit_small': r['item__unit_small'],
                'unit_medium': r['item__unit_medium'] or '',
                'unit_medium_qty': r['item__unit_medium_qty'],
                'unit_large': r['item__unit_large'] or '',
                'unit_large_qty': r['item__unit_large_qty'],
                'min_stock': r['item__min_stock'],
                'selling_price': str(r['item__selling_price']),
                'is_active': r['item__is_active'],
                'warehouse_id': r['warehouse_id'],
                'warehouse_code': r['warehouse__code'],
                'warehouse_name': r['warehouse__name'],
                'total_quantity': r['total_quantity'] or 0,
            }
            for r in qs
        ]
        return Response(result)


# ── Batches (stock-in history) ─────────────────────────────────────────────

class InventoryBatchListView(APIView):
    def get(self, request):
        qs = InventoryBatch.objects.select_related('item', 'warehouse', 'created_by')
        if item_id := request.GET.get('item_id'):
            qs = qs.filter(item_id=item_id)
        if wh_id := request.GET.get('warehouse_id'):
            qs = qs.filter(warehouse_id=wh_id)
        return Response(InventoryBatchSerializer(qs.order_by('-input_date', '-created_at'), many=True).data)


# ── Stock In ───────────────────────────────────────────────────────────────

class StockInView(APIView):
    def post(self, request):
        data = request.data
        try:
            item = InventoryItem.objects.get(pk=data['item_id'])
            warehouse = Warehouse.objects.get(pk=data['warehouse_id'])
        except (InventoryItem.DoesNotExist, Warehouse.DoesNotExist, KeyError):
            return Response({'error': 'Invalid item or warehouse.'}, status=status.HTTP_400_BAD_REQUEST)
        if item.is_service:
            return Response({'error': 'Service items have no physical stock.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            qty_raw = int(data['quantity'])
            unit = data.get('unit', 'small')
            qty_small = _to_small(item, qty_raw, unit)
            value = Decimal(str(data['value']))
            input_date = data['input_date']
        except (KeyError, ValueError) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if qty_small <= 0:
            return Response({'error': 'Quantity must be positive.'}, status=status.HTTP_400_BAD_REQUEST)

        batch = InventoryBatch.objects.create(
            item=item,
            warehouse=warehouse,
            input_date=input_date,
            quantity_initial=qty_small,
            quantity_remaining=qty_small,
            value=value,
            created_by=_actor(request),
        )
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='InventoryBatch',
            entity_id=str(batch.id),
            description=(
                f'Stock in: {qty_small} {item.unit_small} of '
                f'[{item.code}] {item.name} @ {warehouse.name}'
            ),
        )
        return Response(InventoryBatchSerializer(batch).data, status=status.HTTP_201_CREATED)


# ── Stock Out (FIFO) ───────────────────────────────────────────────────────

class StockOutView(APIView):
    def post(self, request):
        data = request.data
        try:
            item = InventoryItem.objects.get(pk=data['item_id'])
            warehouse = Warehouse.objects.get(pk=data['warehouse_id'])
            qty_raw = int(data['quantity'])
            unit = data.get('unit', 'small')
            qty_small = _to_small(item, qty_raw, unit)
        except (InventoryItem.DoesNotExist, Warehouse.DoesNotExist, KeyError, ValueError) as exc:
            return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        if item.is_service:
            return Response({'error': 'Service items have no physical stock.'}, status=status.HTTP_400_BAD_REQUEST)

        if qty_small <= 0:
            return Response({'error': 'Quantity must be positive.'}, status=status.HTTP_400_BAD_REQUEST)

        available = (
            InventoryBatch.objects
            .filter(item=item, warehouse=warehouse, quantity_remaining__gt=0)
            .aggregate(total=Sum('quantity_remaining'))['total'] or 0
        )
        if available < qty_small:
            return Response(
                {
                    'error': (
                        f'Insufficient stock. Available: {available} {item.unit_small}, '
                        f'requested: {qty_small} {item.unit_small}.'
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        shortfall, _cogs = _fifo_deduct(item.id, warehouse.id, qty_small)
        if shortfall > 0:
            return Response(
                {'error': 'Stock deduction failed due to a concurrent modification.'},
                status=status.HTTP_409_CONFLICT,
            )

        notes = data.get('notes', '')
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='StockOut',
            entity_id=str(item.id),
            description=(
                f'Stock out: {qty_small} {item.unit_small} of '
                f'[{item.code}] {item.name} @ {warehouse.name}'
                + (f'. Notes: {notes}' if notes else '')
            ),
        )
        return Response({'deducted': qty_small, 'unit': item.unit_small})


# ── Cashier sync ───────────────────────────────────────────────────────────

class ItemSyncView(APIView):
    """
    GET /api/inventory/sync/items/?since=<ISO-8601>&page=<n>&page_size=<n>

    Returns inventory items updated since the given timestamp, paginated.
    Omit `since` for a full sync (first-time setup).
    Default page_size=200, max=1000.
    """
    def get(self, request):
        from django.utils.dateparse import parse_datetime
        synced_at = timezone.now()
        since_raw = request.GET.get('since')

        try:
            page = max(1, int(request.GET.get('page', 1)))
            page_size = min(max(1, int(request.GET.get('page_size', 200))), 1000)
        except (ValueError, TypeError):
            return Response(
                {'error': 'Invalid `page` or `page_size` value.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        qs = InventoryItem.objects.all().order_by('updated_at', 'id')

        if since_raw:
            try:
                since_dt = parse_datetime(since_raw)
                if since_dt is None:
                    return Response(
                        {'error': 'Invalid `since` value. Use ISO 8601 format, e.g. 2025-01-01T00:00:00Z.'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if timezone.is_naive(since_dt):
                    since_dt = timezone.make_aware(since_dt)
                qs = qs.filter(updated_at__gte=since_dt)
            except (ValueError, TypeError):
                return Response(
                    {'error': 'Invalid `since` value. Use ISO 8601 format, e.g. 2025-01-01T00:00:00Z.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        total_count = qs.count()
        start = (page - 1) * page_size
        end = start + page_size
        items = ItemSyncSerializer(qs[start:end], many=True).data
        has_more = end < total_count

        return Response({
            'synced_at': synced_at,
            'count': len(items),
            'total_count': total_count,
            'has_more': has_more,
            'next_page': page + 1 if has_more else None,
            'items': items,
        })


# ── Helpers ────────────────────────────────────────────────────────────────

def _to_small(item: InventoryItem, qty: int, unit: str) -> int:
    if unit == 'large':
        if not item.unit_large or not item.unit_large_qty:
            raise ValueError('This item has no large unit defined.')
        if not item.unit_medium or not item.unit_medium_qty:
            raise ValueError('Large unit requires a medium unit to be defined.')
        return qty * item.unit_large_qty * item.unit_medium_qty
    if unit == 'medium':
        if not item.unit_medium or not item.unit_medium_qty:
            raise ValueError('This item has no medium unit defined.')
        return qty * item.unit_medium_qty
    return qty


@transaction.atomic
def _fifo_deduct(item_id: int, warehouse_id: int, quantity: int) -> tuple:
    """Deduct stock FIFO. Returns (shortfall, cogs_amount). shortfall=0 means fully deducted."""
    batches = (
        InventoryBatch.objects
        .select_for_update()
        .filter(item_id=item_id, warehouse_id=warehouse_id, quantity_remaining__gt=0)
        .order_by('input_date', 'created_at')
    )
    remaining = quantity
    cogs = Decimal('0')
    for batch in batches:
        if remaining <= 0:
            break
        deduct = min(batch.quantity_remaining, remaining)
        if batch.quantity_initial:
            cogs += (batch.value / Decimal(batch.quantity_initial)) * deduct
        batch.quantity_remaining -= deduct
        batch.save(update_fields=['quantity_remaining'])
        remaining -= deduct
    return remaining, cogs


@transaction.atomic
def _fifo_restock(item_id: int, warehouse_id: int, quantity: int) -> Decimal:
    """Return stock to batches (newest-first). Returns the COGS amount reversed."""
    batches = (
        InventoryBatch.objects
        .select_for_update()
        .filter(item_id=item_id, warehouse_id=warehouse_id)
        .order_by('-input_date', '-created_at')
    )
    remaining = quantity
    cogs = Decimal('0')
    for batch in batches:
        if remaining <= 0:
            break
        capacity = (batch.quantity_initial or 0) - batch.quantity_remaining
        if capacity <= 0:
            continue
        restock = min(capacity, remaining)
        if batch.quantity_initial:
            cogs += (batch.value / Decimal(batch.quantity_initial)) * restock
        batch.quantity_remaining += restock
        batch.save(update_fields=['quantity_remaining'])
        remaining -= restock
    return cogs


# ── Excel template + import ───────────────────────────────────────────────────

class InventoryItemTemplateView(APIView):
    """GET — download a blank .xlsx template for bulk item import."""

    def get(self, request):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Items'

        headers = [
            'code', 'name', 'selling_price',
            'unit_small', 'unit_medium', 'unit_medium_qty',
            'unit_large', 'unit_large_qty',
            'min_stock', 'is_active',
            'category', 'legal_code',
        ]
        ws.append(headers)
        for cell in ws[1]:
            cell.font      = _HEADER_FONT
            cell.fill      = _HEADER_FILL
            cell.alignment = Alignment(horizontal='center')

        # Notes row explaining each column
        notes = [
            'Unique item code (required)',
            'Item name (required)',
            'Selling price (required, e.g. 85000)',
            'Smallest unit, e.g. ml or pcs (required)',
            'Medium unit, e.g. bottle (optional)',
            'Small units per 1 medium unit (optional)',
            'Large unit, e.g. box (optional)',
            'Medium units per 1 large unit (optional)',
            'Minimum stock level (optional, default 0)',
            'yes / no (optional, default yes)',
            'Item category (optional)',
            'Legal/regulatory code (optional)',
        ]
        ws.append(notes)
        note_font = Font(italic=True, color='595959')
        for cell in ws[2]:
            cell.font      = note_font
            cell.alignment = Alignment(wrap_text=True)

        # Example data rows
        examples = [
            ('ITEM-001', 'Vitamin C Serum',  85000,  'ml', 'bottle', 30,  '',    '',  10, 'yes', 'Skincare', ''),
            ('ITEM-002', 'Facial Toner',     45000,  'ml', 'bottle', 200, 'box', 6,   5,  'yes', 'Skincare', 'REG-001'),
            ('ITEM-003', 'Sunscreen SPF50',  120000, 'g',  '',       '',  '',    '',  0,  'yes', 'Suncare',  ''),
        ]
        for row in examples:
            ws.append(list(row))

        # Column widths
        col_widths = [14, 28, 16, 12, 14, 18, 12, 16, 12, 12, 18, 18]
        for i, width in enumerate(col_widths, start=1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = width

        ws.row_dimensions[2].height = 36

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        response = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = 'attachment; filename="inventory_items_template.xlsx"'
        return response


def _parse_import_rows(rows):
    """
    Parse spreadsheet rows (skipping header row 1) into validated item dicts.
    Returns (valid_rows, errors).
    valid_rows: list of dicts with keys matching InventoryItem fields plus 'row' and 'action'.
    errors: list of {row, message}.
    """
    def _int_or_none(v):
        if v is None or str(v).strip() == '':
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    existing_codes = set(InventoryItem.objects.values_list('code', flat=True))
    valid_rows = []
    errors = []

    for i, row in enumerate(rows[1:], start=2):
        padded = list(row) + [None] * 12
        code           = str(padded[0]).strip() if padded[0] is not None else ''
        name           = str(padded[1]).strip() if padded[1] is not None else ''
        price_raw      = padded[2]
        unit_small     = str(padded[3]).strip() if padded[3] is not None else ''
        unit_medium    = str(padded[4]).strip() if padded[4] is not None else ''
        unit_medium_qty_raw = padded[5]
        unit_large     = str(padded[6]).strip() if padded[6] is not None else ''
        unit_large_qty_raw  = padded[7]
        min_stock_raw  = padded[8]
        active_raw     = padded[9]
        category       = str(padded[10]).strip() if padded[10] is not None else ''
        legal_code     = str(padded[11]).strip() if padded[11] is not None else None
        if legal_code == '':
            legal_code = None

        if not code:
            errors.append({'row': i, 'message': 'Code is required.'})
            continue
        if not name:
            errors.append({'row': i, 'message': f'Row {i}: name is required.'})
            continue
        if not unit_small:
            errors.append({'row': i, 'message': f'Row {i}: unit_small is required.'})
            continue
        try:
            selling_price = Decimal(str(price_raw)) if price_raw not in (None, '') else None
            if selling_price is None:
                raise ValueError
        except (ValueError, TypeError):
            errors.append({'row': i, 'message': f'Row {i}: invalid selling_price "{price_raw}".'})
            continue

        unit_medium_qty = _int_or_none(unit_medium_qty_raw)
        unit_large_qty  = _int_or_none(unit_large_qty_raw)
        min_stock       = _int_or_none(min_stock_raw) or 0

        if active_raw is None or str(active_raw).strip() == '':
            is_active = True
        else:
            is_active = str(active_raw).strip().lower() in ('yes', 'true', '1')

        valid_rows.append({
            'row': i,
            'action': 'update' if code in existing_codes else 'create',
            'code': code,
            'name': name,
            'selling_price': selling_price,
            'unit_small': unit_small,
            'unit_medium': unit_medium,
            'unit_medium_qty': unit_medium_qty,
            'unit_large': unit_large,
            'unit_large_qty': unit_large_qty,
            'min_stock': min_stock,
            'is_active': is_active,
            'category': category,
            'legal_code': legal_code,
        })

    return valid_rows, errors


def _load_workbook_from_request(request):
    uploaded = request.FILES.get('file')
    if not uploaded:
        return None, None, Response({'error': 'No file uploaded.'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
    except Exception:
        return None, None, Response({'error': 'Could not parse file. Upload a valid .xlsx file.'}, status=status.HTTP_400_BAD_REQUEST)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return None, None, Response({'error': 'File is empty.'}, status=status.HTTP_400_BAD_REQUEST)
    return wb, rows, None


class InventoryItemImportPreviewView(APIView):
    """
    POST multipart/form-data with file=<xlsx>
    Parses and validates the file without writing to the database.
    Returns { rows: [{row, action, code, name, ...}], errors: [{row, message}] }
    where action is 'create' or 'update'.
    """

    def post(self, request):
        _wb, rows, err = _load_workbook_from_request(request)
        if err:
            return err
        valid_rows, errors = _parse_import_rows(rows)
        preview_rows = [
            {
                'row': r['row'],
                'action': r['action'],
                'code': r['code'],
                'name': r['name'],
                'selling_price': str(r['selling_price']),
                'unit_small': r['unit_small'],
                'unit_medium': r['unit_medium'],
                'category': r['category'],
                'legal_code': r['legal_code'],
                'is_active': r['is_active'],
            }
            for r in valid_rows
        ]
        return Response({'rows': preview_rows, 'errors': errors}, status=status.HTTP_200_OK)


class InventoryItemImportView(APIView):
    """
    POST multipart/form-data with file=<xlsx>
    Columns (row 1 = header, skipped):
      code | name | selling_price | unit_small | unit_medium | unit_medium_qty | unit_large | unit_large_qty | min_stock | is_active | category | legal_code
    Parses, validates, then commits all valid rows.
    Returns { created, updated, errors: [{row, message}] }
    """

    def post(self, request):
        _wb, rows, err = _load_workbook_from_request(request)
        if err:
            return err
        valid_rows, errors = _parse_import_rows(rows)
        created = updated = 0

        for r in valid_rows:
            try:
                with transaction.atomic():
                    obj, was_created = InventoryItem.objects.update_or_create(
                        code=r['code'],
                        defaults={
                            'name': r['name'],
                            'selling_price': r['selling_price'],
                            'unit_small': r['unit_small'],
                            'unit_medium': r['unit_medium'],
                            'unit_medium_qty': r['unit_medium_qty'],
                            'unit_large': r['unit_large'],
                            'unit_large_qty': r['unit_large_qty'],
                            'min_stock': r['min_stock'],
                            'is_active': r['is_active'],
                            'category': r['category'],
                            'legal_code': r['legal_code'],
                        },
                    )
                    AuditLog.objects.create(
                        performed_by=_actor(request),
                        action='CREATE' if was_created else 'UPDATE',
                        entity_type='InventoryItem',
                        entity_id=str(obj.id),
                        description=f'Item {"imported" if was_created else "updated via import"}: {obj.name} ({obj.code})',
                    )
                if was_created:
                    created += 1
                else:
                    updated += 1
            except Exception as exc:
                errors.append({'row': r['row'], 'message': f'Row {r["row"]}: {exc}'})

        return Response({'created': created, 'updated': updated, 'errors': errors}, status=status.HTTP_200_OK)
