import io
import logging
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import InvoiceCreateSerializer, InvoiceReadSerializer, InvoiceUpdateSerializer
from ..models import AppUser, AuditLog, ChartOfAccounts, InventoryItem, Invoice, InvoiceItem, Patient, PromotionUsage, Warehouse
from .crm_page import refresh_crm_profile
from .promotion_page import validate_promotion

logger = logging.getLogger(__name__)

# ── Shared Excel column layout ────────────────────────────────────────────────
# Sheet "Invoices": invoice_number | datetime | patient_no | payment_method |
#                   discount | tax | additional_charges | grand_total |
#                   cashier_id | warehouse_id
# Sheet "Items":    invoice_number | item_code | item_name | quantity | price
# ─────────────────────────────────────────────────────────────────────────────

INV_HEADERS  = ['invoice_number','datetime','patient_no','payment_method',
                'discount','tax','additional_charges','grand_total',
                'cashier_id','warehouse_id']
ITEM_HEADERS = ['invoice_number','item_code','item_name','quantity','price']


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


class InvoiceCreateView(APIView):
    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        serializer = InvoiceCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        # ── Resolve FKs ───────────────────────────────────────────────────────

        patient = None
        if data.get('patient_no'):
            try:
                patient = Patient.objects.get(patient_no=data['patient_no'])
            except Patient.DoesNotExist:
                return Response(
                    {'patient_no': 'Patient not found.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        cashier = None
        if data.get('cashier_id'):
            try:
                cashier = AppUser.objects.get(id=data['cashier_id'])
            except AppUser.DoesNotExist:
                return Response(
                    {'cashier_id': 'Cashier not found.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        warehouse = None
        if data.get('warehouse_id'):
            try:
                warehouse = Warehouse.objects.get(id=data['warehouse_id'])
            except Warehouse.DoesNotExist:
                return Response(
                    {'warehouse_id': 'Warehouse not found.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        try:
            payment_account = ChartOfAccounts.objects.get(
                id=data['payment_method_id'],
                account_number__gte=1100000,
                account_number__lte=1199999,
            )
        except ChartOfAccounts.DoesNotExist:
            return Response(
                {'payment_method_id': 'Cash/cash-equivalent account not found.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Validate all items exist before writing anything ──────────────────

        item_ids = [i['item_id'] for i in data['items']]
        items_by_id = {
            obj.id: obj
            for obj in InventoryItem.objects.filter(id__in=item_ids)
        }
        missing = [iid for iid in item_ids if iid not in items_by_id]
        if missing:
            return Response(
                {'items': f"Item IDs not found: {missing}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # ── Create Invoice ────────────────────────────────────────────────────

        invoice = Invoice.objects.create(
            datetime=data.get('datetime') or timezone.now(),
            patient_no=patient,
            payment_method=payment_account,
            discount=data['discount'],
            cashier=cashier,
            warehouse=warehouse,
            tax=data['tax'],
            additional_charges=data['additional_charges'],
            grand_total=data['grand_total'],
        )

        # ── Create InvoiceItems ───────────────────────────────────────────────

        InvoiceItem.objects.bulk_create([
            InvoiceItem(
                invoice=invoice,
                item=items_by_id[i['item_id']],
                quantity=i['quantity'],
                price=i['price'],
            )
            for i in data['items']
        ])

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='Invoice',
            entity_id=str(invoice.id),
            description=f'Invoice {invoice.invoice_number} created, {len(data["items"])} item(s), total {invoice.grand_total}',
        )

        # ── Promotion ─────────────────────────────────────────────────────────

        promotion_code = (request.data.get('promotion_code') or '').strip()
        if promotion_code:
            promo, discount_amount, error = validate_promotion(
                promotion_code, patient, data['grand_total']
            )
            if promo:
                PromotionUsage.objects.create(
                    promotion=promo,
                    patient_no=patient,
                    invoice=invoice,
                    discount_applied=discount_amount,
                )
                invoice.promotion = promo
                invoice.save(update_fields=['promotion'])
            else:
                logger.warning(
                    'Promotion code %r skipped on invoice %s: %s',
                    promotion_code, invoice.invoice_number, error,
                )

        # ── CRM refresh ───────────────────────────────────────────────────────

        if patient is not None:
            refresh_crm_profile(patient)

        invoice = (
            Invoice.objects
            .select_related('patient_no', 'cashier', 'warehouse', 'payment_method')
            .prefetch_related('items__item')
            .get(pk=invoice.pk)
        )
        return Response(
            InvoiceReadSerializer(invoice).data,
            status=status.HTTP_201_CREATED,
        )


class InvoiceListView(APIView):
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            Invoice.objects
            .select_related('patient_no', 'cashier', 'warehouse', 'payment_method')
            .prefetch_related('items__item')
            .order_by('-datetime')
        )
        # Optional filters
        if q := request.GET.get('q', '').strip():
            from django.db.models import Q
            qs = qs.filter(
                Q(invoice_number__icontains=q) |
                Q(patient_no__name__icontains=q)
            )
        if method := request.GET.get('payment_method', '').strip():
            qs = qs.filter(payment_method_id=method)
        return Response(InvoiceReadSerializer(qs, many=True).data)


class InvoiceDetailView(APIView):
    permission_classes = [AllowAny]

    def _get(self, pk):
        try:
            return (
                Invoice.objects
                .select_related('patient_no', 'cashier', 'warehouse', 'payment_method')
                .prefetch_related('items__item')
                .get(pk=pk)
            )
        except Invoice.DoesNotExist:
            return None

    def get(self, request, pk):
        invoice = self._get(pk)
        if invoice is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(InvoiceReadSerializer(invoice).data)

    @transaction.atomic
    def patch(self, request, pk):
        return self.put(request, pk)

    @transaction.atomic
    def put(self, request, pk):
        invoice = self._get(pk)
        if invoice is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = InvoiceUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        changes = []

        # ── Scalar fields ─────────────────────────────────────────────────────

        if 'datetime' in data:
            invoice.datetime = data['datetime']
            changes.append('datetime')

        if 'payment_method_id' in data:
            if data['payment_method_id']:
                try:
                    invoice.payment_method = ChartOfAccounts.objects.get(
                        id=data['payment_method_id'],
                        account_number__gte=1100000,
                        account_number__lte=1199999,
                    )
                except ChartOfAccounts.DoesNotExist:
                    return Response({'payment_method_id': 'Cash/cash-equivalent account not found.'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                invoice.payment_method = None
            changes.append('payment_method')

        for field in ('discount', 'tax', 'additional_charges', 'grand_total'):
            if field in data:
                setattr(invoice, field, data[field])
                changes.append(field)

        # ── Patient ───────────────────────────────────────────────────────────

        if 'patient_no' in data:
            if data['patient_no']:
                try:
                    invoice.patient_no = Patient.objects.get(patient_no=data['patient_no'])
                except Patient.DoesNotExist:
                    return Response({'patient_no': 'Patient not found.'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                invoice.patient_no = None
            changes.append('patient_no')

        # ── Cashier ───────────────────────────────────────────────────────────

        if 'cashier_id' in data:
            if data['cashier_id']:
                try:
                    invoice.cashier = AppUser.objects.get(id=data['cashier_id'])
                except AppUser.DoesNotExist:
                    return Response({'cashier_id': 'Cashier not found.'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                invoice.cashier = None
            changes.append('cashier')

        # ── Warehouse ─────────────────────────────────────────────────────────

        if 'warehouse_id' in data:
            if data['warehouse_id']:
                try:
                    invoice.warehouse = Warehouse.objects.get(id=data['warehouse_id'])
                except Warehouse.DoesNotExist:
                    return Response({'warehouse_id': 'Warehouse not found.'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                invoice.warehouse = None
            changes.append('warehouse')

        invoice.save()

        # ── Line items (replace-all strategy) ────────────────────────────────

        if 'items' in data:
            item_ids = [i['item_id'] for i in data['items']]
            items_by_id = {
                obj.id: obj
                for obj in InventoryItem.objects.filter(id__in=item_ids)
            }
            missing = [iid for iid in item_ids if iid not in items_by_id]
            if missing:
                return Response({'items': f'Item IDs not found: {missing}'}, status=status.HTTP_400_BAD_REQUEST)

            invoice.items.all().delete()
            InvoiceItem.objects.bulk_create([
                InvoiceItem(
                    invoice=invoice,
                    item=items_by_id[i['item_id']],
                    quantity=i['quantity'],
                    price=i['price'],
                )
                for i in data['items']
            ])
            changes.append('items')

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='Invoice',
            entity_id=str(invoice.id),
            description=f'Invoice {invoice.invoice_number} updated — fields changed: {", ".join(changes)}',
        )

        invoice.refresh_from_db()
        return Response(InvoiceReadSerializer(
            Invoice.objects
            .select_related('patient_no', 'cashier', 'warehouse')
            .prefetch_related('items__item')
            .get(pk=pk)
        ).data)


class InvoiceExportView(APIView):
    """GET /api/invoices/export/  — download filtered invoices as .xlsx"""
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            Invoice.objects
            .select_related('patient_no', 'cashier', 'warehouse', 'payment_method')
            .prefetch_related('items__item')
            .order_by('-datetime')
        )
        if q := request.GET.get('q', '').strip():
            from django.db.models import Q
            qs = qs.filter(
                Q(invoice_number__icontains=q) |
                Q(patient_no__name__icontains=q)
            )
        if method := request.GET.get('payment_method', '').strip():
            qs = qs.filter(payment_method_id=method)

        wb = openpyxl.Workbook()
        ws_inv  = wb.active
        ws_inv.title = 'Invoices'
        ws_item = wb.create_sheet('Items')

        ws_inv.append(INV_HEADERS)
        ws_item.append(ITEM_HEADERS)

        for inv in qs:
            ws_inv.append([
                inv.invoice_number,
                inv.datetime.strftime('%Y-%m-%dT%H:%M:%S') if inv.datetime else '',
                inv.patient_no_id or '',
                inv.payment_method.name if inv.payment_method else '',
                float(inv.discount),
                float(inv.tax),
                float(inv.additional_charges),
                float(inv.grand_total),
                inv.cashier_id or '',
                inv.warehouse.name if inv.warehouse else '',
            ])
            for item in inv.items.all():
                ws_item.append([
                    inv.invoice_number,
                    item.item.code,
                    item.item.name,
                    float(item.quantity),
                    float(item.price),
                ])

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        filename = f"invoices_{timezone.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        response = HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        )
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class InvoiceImportView(APIView):
    """POST /api/invoices/import/  — upload .xlsx, create invoices that don't exist yet"""
    permission_classes = [AllowAny]

    @transaction.atomic
    def post(self, request):
        upload = request.FILES.get('file')
        if not upload:
            return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            wb = openpyxl.load_workbook(upload, read_only=True, data_only=True)
        except Exception as e:
            return Response({'error': f'Cannot read workbook: {e}'}, status=status.HTTP_400_BAD_REQUEST)

        if 'Invoices' not in wb.sheetnames or 'Items' not in wb.sheetnames:
            return Response(
                {'error': 'Workbook must contain sheets named "Invoices" and "Items".'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ws_inv  = wb['Invoices']
        ws_item = wb['Items']

        # Parse Invoices sheet (skip header row 1)
        inv_rows = []
        for row in ws_inv.iter_rows(min_row=2, values_only=True):
            if not row[0]:
                continue
            inv_rows.append({
                'invoice_number':    str(row[0]).strip(),
                'datetime':          str(row[1]).strip() if row[1] else '',
                'patient_no':        str(row[2]).strip() if row[2] else '',
                'payment_method':    str(row[3]).strip() if row[3] else '',
                'discount':          _to_dec(row[4]),
                'tax':               _to_dec(row[5]),
                'additional_charges':_to_dec(row[6]),
                'grand_total':       _to_dec(row[7]),
                'cashier_id':        str(row[8]).strip() if row[8] else '',
                'warehouse_id':      str(row[9]).strip() if row[9] else '',
            })

        # Parse Items sheet keyed by invoice_number
        items_by_inv: dict[str, list] = {}
        for row in ws_item.iter_rows(min_row=2, values_only=True):
            if not row[0]:
                continue
            inv_no = str(row[0]).strip()
            items_by_inv.setdefault(inv_no, []).append({
                'item_code': str(row[1]).strip() if row[1] else '',
                'quantity':  _to_dec(row[3]),
                'price':     _to_dec(row[4]),
            })

        created = []
        skipped = []
        errors  = []

        for row in inv_rows:
            inv_no = row['invoice_number']

            if Invoice.objects.filter(invoice_number=inv_no).exists():
                skipped.append(inv_no)
                continue

            # Resolve optional FKs — soft-fail: skip unknown references
            patient = None
            if row['patient_no']:
                patient = Patient.objects.filter(patient_no=row['patient_no']).first()

            cashier = None
            if row['cashier_id'] and str(row['cashier_id']).isdigit():
                cashier = AppUser.objects.filter(id=int(row['cashier_id'])).first()

            warehouse = None
            if row['warehouse_id']:
                warehouse = Warehouse.objects.filter(name=row['warehouse_id']).first()

            payment = ChartOfAccounts.objects.filter(
                name=row['payment_method'],
                account_number__gte=1100000,
                account_number__lte=1199999,
            ).first()

            try:
                dt = timezone.datetime.fromisoformat(row['datetime']) if row['datetime'] else timezone.now()
                if timezone.is_naive(dt):
                    dt = timezone.make_aware(dt)
            except ValueError:
                dt = timezone.now()

            line_dicts = items_by_inv.get(inv_no, [])
            if not line_dicts:
                errors.append(f'{inv_no}: no items found in Items sheet')
                continue

            # Resolve item codes to InventoryItem objects
            resolved_lines = []
            row_error = False
            for ld in line_dicts:
                item_obj = InventoryItem.objects.filter(code=ld['item_code']).first()
                if item_obj is None:
                    errors.append(f'{inv_no}: item code "{ld["item_code"]}" not found — row skipped')
                    row_error = True
                    break
                resolved_lines.append((item_obj, ld['quantity'], ld['price']))

            if row_error:
                continue

            invoice = Invoice(
                datetime=dt,
                patient_no=patient,
                payment_method=payment,
                discount=row['discount'],
                cashier=cashier,
                warehouse=warehouse,
                tax=row['tax'],
                additional_charges=row['additional_charges'],
                grand_total=row['grand_total'],
            )
            invoice.invoice_number = inv_no  # preserve original number
            invoice.save()

            InvoiceItem.objects.bulk_create([
                InvoiceItem(invoice=invoice, item=item_obj, quantity=qty, price=price)
                for item_obj, qty, price in resolved_lines
            ])

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='IMPORT',
                entity_type='Invoice',
                entity_id=str(invoice.id),
                description=f'Invoice {inv_no} imported via Excel upload',
            )
            created.append(inv_no)

        return Response({
            'created': len(created),
            'skipped': len(skipped),
            'errors':  errors,
            'created_numbers': created,
            'skipped_numbers': skipped,
        }, status=status.HTTP_200_OK)


def _to_dec(val) -> Decimal:
    try:
        return Decimal(str(val)).quantize(Decimal('0.01'))
    except (InvalidOperation, TypeError):
        return Decimal('0.00')
