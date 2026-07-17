import io
import logging
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.db.models import F
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import InvoiceCreateSerializer, InvoiceReadSerializer, InvoiceUpdateSerializer
from ..models import (
    AppUser, AuditLog, ChartOfAccounts, InventoryItem, Invoice, InvoiceItem,
    LedgerEntry, Patient, PatientPackage, PatientPackageRedemption, PromotionUsage,
    Treatment, TreatmentPackage, Warehouse,
)
from .crm_page import refresh_crm_profile
from .inventory_page import _fifo_deduct, _fifo_deduct_global, _fifo_restock, _fifo_restock_global
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


def _le(account, entry_type, amount, invoice, description):
    """Create a single LedgerEntry row."""
    LedgerEntry.objects.create(
        account=account,
        date=invoice.datetime.date(),
        description=description,
        entry_type=entry_type,
        amount=amount,
        invoice=invoice,
    )


def _post_accounting(invoice, line_items, items_by_id):
    """
    Post accounting entries for a completed invoice:
      - Cash/payment account  += grand_total  (DEBIT asset)
      - Per line: revenue account for the item's category += price * qty  (CREDIT revenue)
      - Per physical line (is_service=False): FIFO deduct stock, then
        inventory asset -= COGS  (CREDIT asset),  COGS account += COGS  (DEBIT COGS)
    Falls back to system accounts (4200000, 5100000, 1300000) when an item
    has no item_category assigned.
    Each balance change is mirrored as a LedgerEntry row for historical reporting.
    """
    inv_no = invoice.invoice_number

    if invoice.payment_method_id:
        ChartOfAccounts.objects.filter(pk=invoice.payment_method_id).update(
            balance=F('balance') + invoice.grand_total
        )
        _le(invoice.payment_method, 'debit', invoice.grand_total, invoice,
            f'Invoice {inv_no} – Payment received')

    fallback_revenue = ChartOfAccounts.objects.filter(account_number=4200000).first()
    fallback_cogs = ChartOfAccounts.objects.filter(account_number=5100000).first()
    inventory_asset = ChartOfAccounts.objects.filter(account_number=1300000).first()

    for line in line_items:
        if not line.get('item_id'):
            continue
        item = items_by_id[line['item_id']]
        line_revenue = line['price'] * line['quantity']
        cat = item.item_category

        revenue_acct = (cat.revenue_account if cat and cat.revenue_account_id else fallback_revenue)
        cogs_acct = (cat.cogs_account if cat and cat.cogs_account_id else fallback_cogs)

        if revenue_acct:
            ChartOfAccounts.objects.filter(pk=revenue_acct.pk).update(
                balance=F('balance') + line_revenue
            )
            _le(revenue_acct, 'credit', line_revenue, invoice,
                f'Invoice {inv_no} – {item.name}')

        if not item.is_service and invoice.warehouse_id:
            if line['quantity'] > 0:
                _shortfall, cogs_amount = _fifo_deduct(item.id, invoice.warehouse_id, line['quantity'])
                if cogs_amount > 0:
                    if inventory_asset:
                        ChartOfAccounts.objects.filter(pk=inventory_asset.pk).update(
                            balance=F('balance') - cogs_amount
                        )
                        _le(inventory_asset, 'credit', cogs_amount, invoice,
                            f'Invoice {inv_no} – FIFO deduction: {item.name}')
                    if cogs_acct:
                        ChartOfAccounts.objects.filter(pk=cogs_acct.pk).update(
                            balance=F('balance') + cogs_amount
                        )
                        _le(cogs_acct, 'debit', cogs_amount, invoice,
                            f'Invoice {inv_no} – COGS: {item.name}')

        if item.is_service:
            treatment = getattr(item, 'treatment', None)
            if treatment:
                deduct_fn = (
                    (lambda mid, qty: _fifo_deduct(mid, invoice.warehouse_id, qty))
                    if invoice.warehouse_id
                    else (lambda mid, qty: _fifo_deduct_global(mid, qty))
                )
                for material in treatment.materials.all():
                    _shortfall, mat_cogs = deduct_fn(material.item_id, material.quantity_small)
                    if mat_cogs <= 0:
                        continue
                    if inventory_asset:
                        ChartOfAccounts.objects.filter(pk=inventory_asset.pk).update(
                            balance=F('balance') - mat_cogs
                        )
                        _le(inventory_asset, 'credit', mat_cogs, invoice,
                            f'Invoice {inv_no} – material: {material.item.name} for {item.name}')
                    if cogs_acct:
                        ChartOfAccounts.objects.filter(pk=cogs_acct.pk).update(
                            balance=F('balance') + mat_cogs
                        )
                        _le(cogs_acct, 'debit', mat_cogs, invoice,
                            f'Invoice {inv_no} – material COGS: {material.item.name} for {item.name}')


def _reverse_accounting_instances(payment_method_id, grand_total, item_instances, warehouse_id, invoice=None):
    """
    Reverse every accounting entry that _post_accounting originally made,
    using InvoiceItem model instances (with item relations already prefetched).
    Called at the start of a PUT/PATCH so the edit can be re-applied cleanly.
    Each reversal is recorded as a LedgerEntry (opposite entry_type) when invoice is supplied.
    """
    inv_no = invoice.invoice_number if invoice else '?'

    if payment_method_id:
        ChartOfAccounts.objects.filter(pk=payment_method_id).update(
            balance=F('balance') - grand_total
        )
        if invoice:
            payment_acct = ChartOfAccounts.objects.filter(pk=payment_method_id).first()
            if payment_acct:
                _le(payment_acct, 'credit', grand_total, invoice,
                    f'Invoice {inv_no} – Payment correction')

    fallback_revenue = ChartOfAccounts.objects.filter(account_number=4200000).first()
    fallback_cogs    = ChartOfAccounts.objects.filter(account_number=5100000).first()
    inventory_asset  = ChartOfAccounts.objects.filter(account_number=1300000).first()

    for inst in item_instances:
        if not inst.item_id:
            continue
        item = inst.item
        line_revenue = inst.price * inst.quantity
        cat = item.item_category

        revenue_acct = (cat.revenue_account if cat and cat.revenue_account_id else fallback_revenue)
        cogs_acct    = (cat.cogs_account    if cat and cat.cogs_account_id    else fallback_cogs)

        if revenue_acct:
            ChartOfAccounts.objects.filter(pk=revenue_acct.pk).update(
                balance=F('balance') - line_revenue
            )
            if invoice:
                _le(revenue_acct, 'debit', line_revenue, invoice,
                    f'Invoice {inv_no} – Correction: {item.name}')

        if not item.is_service and warehouse_id:
            if inst.quantity > 0:
                cogs_amount = _fifo_restock(item.id, warehouse_id, inst.quantity)
                if cogs_amount > 0:
                    if inventory_asset:
                        ChartOfAccounts.objects.filter(pk=inventory_asset.pk).update(
                            balance=F('balance') + cogs_amount
                        )
                        if invoice:
                            _le(inventory_asset, 'debit', cogs_amount, invoice,
                                f'Invoice {inv_no} – FIFO restock: {item.name}')
                    if cogs_acct:
                        ChartOfAccounts.objects.filter(pk=cogs_acct.pk).update(
                            balance=F('balance') - cogs_amount
                        )
                        if invoice:
                            _le(cogs_acct, 'credit', cogs_amount, invoice,
                                f'Invoice {inv_no} – COGS correction: {item.name}')

        # Mirror of the material deduction _post_accounting does for services.
        if item.is_service:
            treatment = getattr(item, 'treatment', None)
            if treatment:
                restock_fn = (
                    (lambda mid, qty: _fifo_restock(mid, warehouse_id, qty))
                    if warehouse_id
                    else (lambda mid, qty: _fifo_restock_global(mid, qty))
                )
                for material in treatment.materials.all():
                    mat_cogs = restock_fn(material.item_id, material.quantity_small)
                    if mat_cogs <= 0:
                        continue
                    if inventory_asset:
                        ChartOfAccounts.objects.filter(pk=inventory_asset.pk).update(
                            balance=F('balance') + mat_cogs
                        )
                        if invoice:
                            _le(inventory_asset, 'debit', mat_cogs, invoice,
                                f'Invoice {inv_no} – material restock: {material.item.name} for {item.name}')
                    if cogs_acct:
                        ChartOfAccounts.objects.filter(pk=cogs_acct.pk).update(
                            balance=F('balance') - mat_cogs
                        )
                        if invoice:
                            _le(cogs_acct, 'credit', mat_cogs, invoice,
                                f'Invoice {inv_no} – material COGS correction: {material.item.name} for {item.name}')


def _handle_packages(invoice, patient, line_items, items_by_id):
    """Process package sales and redemptions for a saved invoice.

    Silently skips redemptions whose package has no remaining sessions for the
    requested treatment — the line still posts at the price the cashier sent
    (cashier sees remaining count in the UI, so this is a defensive fallback,
    not an authorization check).
    """
    # ── Sales ──
    if patient is not None:
        package_by_catalog = {
            tp.catalog_item_id: tp
            for tp in TreatmentPackage.objects.filter(
                catalog_item_id__in=[i for i in items_by_id]
            )
        }
        for line in line_items:
            if not line.get('item_id'):
                continue
            tp = package_by_catalog.get(line['item_id'])
            if not tp:
                continue
            qty = int(line['quantity'].to_integral_value())
            for _ in range(max(qty, 0)):
                PatientPackage.objects.create(
                    patient=patient,
                    package=tp,
                    purchased_invoice=invoice,
                )

    # ── Redemptions ──
    touched = set()
    for line in line_items:
        pp_id = line.get('redeem_patient_package_id')
        treatment_id = line.get('treatment_id')
        if not pp_id or not treatment_id:
            continue
        try:
            pp = PatientPackage.objects.select_related('package').get(pk=pp_id)
        except PatientPackage.DoesNotExist:
            continue
        if pp.remaining_for(treatment_id) <= 0:
            continue
        try:
            treatment = Treatment.objects.get(pk=treatment_id)
        except Treatment.DoesNotExist:
            continue
        qty = max(int(line['quantity'].to_integral_value()), 1)
        for _ in range(qty):
            if pp.remaining_for(treatment_id) <= 0:
                break
            PatientPackageRedemption.objects.create(
                patient_package=pp,
                treatment=treatment,
                invoice=invoice,
            )
        touched.add(pp.id)

    for pp_id in touched:
        try:
            PatientPackage.objects.get(pk=pp_id).refresh_status()
        except PatientPackage.DoesNotExist:
            pass


def _foreign_redemptions_exist(invoice):
    """
    True when a package sold by this invoice has been redeemed on a *different*
    invoice. Reversing the sale would cascade-delete those redemptions, so the
    edit is refused instead.
    """
    return (
        PatientPackageRedemption.objects
        .filter(patient_package__purchased_invoice=invoice)
        .exclude(invoice=invoice)
        .exists()
    )


def _reverse_packages(invoice):
    """
    Undo the package sales and redemptions _handle_packages made for this
    invoice, so they can be re-applied against the edited lines.
    Caller must have checked _foreign_redemptions_exist first.
    """
    touched = set(
        PatientPackageRedemption.objects
        .filter(invoice=invoice)
        .values_list('patient_package_id', flat=True)
    )
    PatientPackageRedemption.objects.filter(invoice=invoice).delete()
    PatientPackage.objects.filter(purchased_invoice=invoice).delete()

    for pp in PatientPackage.objects.filter(pk__in=touched):
        pp.refresh_status()


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

        payment_account = None
        if data.get('payment_method_id'):
            try:
                payment_account = ChartOfAccounts.objects.get(
                    id=data['payment_method_id'],
                    parent__account_number=1100000,
                    is_head=False,
                )
            except ChartOfAccounts.DoesNotExist:
                return Response(
                    {'payment_method_id': 'Cash/cash-equivalent account not found.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Validate all inventory items exist before writing anything ────────

        item_ids = [i['item_id'] for i in data['items'] if i.get('item_id')]
        items_by_id = {
            obj.id: obj
            for obj in InventoryItem.objects.filter(id__in=item_ids).select_related(
                'item_category__revenue_account',
                'item_category__cogs_account',
            ).prefetch_related('treatment__materials__item')
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
            notes=data.get('notes', ''),
            promotion_code=(data.get('promotion_code') or '').strip(),
        )

        # ── Create InvoiceItems ───────────────────────────────────────────────

        InvoiceItem.objects.bulk_create([
            InvoiceItem(
                invoice=invoice,
                item=items_by_id[i['item_id']] if i.get('item_id') else None,
                item_name=i.get('item_name', '') if not i.get('item_id') else '',
                quantity=i['quantity'],
                price=i['price'],
                discount_pct=i.get('discount_pct', 0),
            )
            for i in data['items']
        ])

        # ── Accounting + stock deduction ──────────────────────────────────────

        _post_accounting(invoice, data['items'], items_by_id)

        # ── Treatment Packages: sales + redemptions ───────────────────────────
        # Sale: any line whose item is a TreatmentPackage.catalog_item creates
        #   one PatientPackage per quantity (rounded down).
        # Redemption: any line with redeem_patient_package_id consumes one
        #   session from that PatientPackage for the given treatment.
        _handle_packages(invoice, patient, data['items'], items_by_id)

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
        if inv_no := request.GET.get('invoice_number', '').strip():
            qs = qs.filter(invoice_number=inv_no)
        elif q := request.GET.get('q', '').strip():
            from django.db.models import Q
            qs = qs.filter(
                Q(invoice_number__icontains=q) |
                Q(patient_no__name__icontains=q) |
                Q(patient_no__patient_no__icontains=q)
            )
        if method := request.GET.get('payment_method', '').strip():
            qs = qs.filter(payment_method_id=method)

        # Hide voided invoices unless the caller explicitly asks to include them.
        if request.GET.get('include_voided', '').strip().lower() not in ('1', 'true', 'yes'):
            qs = qs.filter(is_voided=False)

        total = qs.count()

        try:
            page_size = int(request.GET.get('page_size', 50))
        except (ValueError, TypeError):
            page_size = 50
        page_size = page_size if page_size in (10, 50, 100) else 50

        try:
            page = max(int(request.GET.get('page', 1)), 1)
        except (ValueError, TypeError):
            page = 1

        offset = (page - 1) * page_size
        qs = qs[offset:offset + page_size]

        return Response({
            'count': total,
            'results': InvoiceReadSerializer(qs, many=True).data,
        })


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
        if invoice.is_voided:
            return Response(
                {'error': 'Cannot edit a voided invoice.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = InvoiceUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        changes = []

        # ── Resolve and validate the new lines before touching anything ───────
        # Everything below this point mutates; a 400 returned after a write
        # would still commit, since only an exception rolls the atomic block back.

        replacing_items = 'items' in data
        new_items_by_id: dict = {}

        if replacing_items:
            item_ids = [i['item_id'] for i in data['items'] if i.get('item_id')]
            new_items_by_id = {
                obj.id: obj
                for obj in InventoryItem.objects.filter(id__in=item_ids).select_related(
                    'item_category__revenue_account',
                    'item_category__cogs_account',
                ).prefetch_related('treatment__materials__item')
            }
            missing = [iid for iid in item_ids if iid not in new_items_by_id]
            if missing:
                return Response({'items': f'Item IDs not found: {missing}'}, status=status.HTTP_400_BAD_REQUEST)

            # Replacing the lines reverses this invoice's package sales. If another
            # invoice already redeemed against one, reversing would destroy it.
            if _foreign_redemptions_exist(invoice):
                return Response(
                    {'items': 'Cannot edit items: a treatment package sold by this '
                              'invoice has already been redeemed on another invoice. '
                              'Void the redeeming invoice first.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Capture old state before any modifications ────────────────────────
        old_grand_total       = invoice.grand_total
        old_payment_method_id = invoice.payment_method_id
        old_warehouse_id      = invoice.warehouse_id
        old_patient           = invoice.patient_no
        old_item_instances    = list(
            invoice.items
            .select_related(
                'item__item_category__revenue_account',
                'item__item_category__cogs_account',
            )
            .prefetch_related('item__treatment__materials__item')
            .all()
        )

        # ── Scalar fields ─────────────────────────────────────────────────────

        if 'datetime' in data:
            invoice.datetime = data['datetime']
            changes.append('datetime')

        if 'payment_method_id' in data:
            if data['payment_method_id']:
                try:
                    invoice.payment_method = ChartOfAccounts.objects.get(
                        id=data['payment_method_id'],
                        parent__account_number=1100000,
                        is_head=False,
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

        new_line_items_data = None  # None = items unchanged

        if replacing_items:
            invoice.items.all().delete()
            InvoiceItem.objects.bulk_create([
                InvoiceItem(
                    invoice=invoice,
                    item=new_items_by_id.get(i['item_id']) if i.get('item_id') else None,
                    item_name=i.get('item_name', '') if not i.get('item_id') else '',
                    quantity=i['quantity'],
                    price=i['price'],
                    discount_pct=i.get('discount_pct', 0),
                )
                for i in data['items']
            ])
            new_line_items_data = data['items']
            changes.append('items')

        # ── Accounting: reverse old entries, re-apply for new state ──────────

        _reverse_accounting_instances(
            old_payment_method_id, old_grand_total, old_item_instances, old_warehouse_id,
            invoice=invoice,
        )

        if new_line_items_data is not None:
            _reverse_packages(invoice)
            _post_accounting(invoice, new_line_items_data, new_items_by_id)
            _handle_packages(invoice, invoice.patient_no, new_line_items_data, new_items_by_id)
        else:
            # Items unchanged — re-apply using old instances converted to dict format
            old_lines_as_dicts = [
                {'item_id': inst.item_id, 'quantity': inst.quantity, 'price': inst.price}
                for inst in old_item_instances
            ]
            old_items_by_id = {
                inst.item_id: inst.item
                for inst in old_item_instances
                if inst.item_id
            }
            _post_accounting(invoice, old_lines_as_dicts, old_items_by_id)

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='Invoice',
            entity_id=str(invoice.id),
            description=f'Invoice {invoice.invoice_number} updated — fields changed: {", ".join(changes)}',
        )

        # ── CRM refresh ───────────────────────────────────────────────────────
        # Both patients when the invoice moved between them.
        for p in {old_patient, invoice.patient_no}:
            if p is not None:
                refresh_crm_profile(p)

        invoice.refresh_from_db()
        return Response(InvoiceReadSerializer(
            Invoice.objects
            .select_related('patient_no', 'cashier', 'warehouse')
            .prefetch_related('items__item')
            .get(pk=pk)
        ).data)

    @transaction.atomic
    def delete(self, request, pk):
        """Void (soft-delete) an invoice: reverse its accounting but keep the record."""
        invoice = self._get(pk)
        if invoice is None:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if invoice.is_voided:
            return Response(
                {'error': 'Invoice is already voided.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_item_instances = list(
            invoice.items
            .select_related(
                'item__item_category__revenue_account',
                'item__item_category__cogs_account',
            )
            .prefetch_related('item__treatment__materials__item')
            .all()
        )

        inv_no = invoice.invoice_number
        patient = invoice.patient_no

        # Post reversing ledger entries (keeps the original posting + reversal as
        # a complete audit trail) and roll the affected account balances back.
        _reverse_accounting_instances(
            invoice.payment_method_id,
            invoice.grand_total,
            old_item_instances,
            invoice.warehouse_id,
            invoice=invoice,
        )

        invoice.is_voided = True
        invoice.voided_at = timezone.now()
        invoice.voided_by = _actor(request)
        invoice.save(update_fields=['is_voided', 'voided_at', 'voided_by'])

        if patient is not None:
            refresh_crm_profile(patient)

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='VOID',
            entity_type='Invoice',
            entity_id=str(pk),
            description=f'Invoice {inv_no} voided — accounting reversed',
        )

        return Response(InvoiceReadSerializer(self._get(pk)).data)


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
                Q(patient_no__name__icontains=q) |
                Q(patient_no__patient_no__icontains=q)
            )
        if method := request.GET.get('payment_method', '').strip():
            qs = qs.filter(payment_method_id=method)

        # Hide voided invoices unless explicitly requested (mirrors the list view).
        if request.GET.get('include_voided', '').strip().lower() not in ('1', 'true', 'yes'):
            qs = qs.filter(is_voided=False)

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
                parent__account_number=1100000,
                is_head=False,
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
