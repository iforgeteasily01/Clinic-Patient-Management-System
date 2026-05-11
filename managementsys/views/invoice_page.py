import logging

from django.db import transaction
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
