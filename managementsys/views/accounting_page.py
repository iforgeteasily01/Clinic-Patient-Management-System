import io
import json
from decimal import Decimal, InvalidOperation

import openpyxl
from django.db import transaction
from django.db.models import F, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import (
    AccountTransferSerializer,
    LedgerEntrySerializer,
    PurchaseInvoiceDetailSerializer,
    PurchaseInvoiceItemSerializer,
    PurchaseInvoiceListSerializer,
    SupplierSerializer,
)
from ..models import (
    AccountTransfer, AppUser, AuditLog, ChartOfAccounts,
    InventoryBatch, InventoryItem, Invoice, LedgerEntry,
    PurchaseAdditionalCost, PurchaseInvoice, PurchaseInvoiceItem, Supplier, Warehouse,
)


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _safe_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError):
        return Decimal('0')


# ── Suppliers ──────────────────────────────────────────────────────────────────

class SupplierListCreateView(APIView):
    def get(self, request):
        qs = Supplier.objects.all()
        q = request.query_params.get('q', '').strip()
        if q:
            qs = qs.filter(name__icontains=q)
        active = request.query_params.get('active', '').strip()
        if active == 'true':
            qs = qs.filter(is_active=True)
        return Response(SupplierSerializer(qs, many=True).data)

    def post(self, request):
        ser = SupplierSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        instance = ser.save()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='Supplier',
            entity_id=str(instance.id),
            description=f'Supplier created: {instance.name}',
        )
        return Response(SupplierSerializer(instance).data, status=status.HTTP_201_CREATED)


class SupplierDetailView(APIView):
    def _get(self, pk):
        try:
            return Supplier.objects.get(pk=pk)
        except Supplier.DoesNotExist:
            return None

    def get(self, request, pk):
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(SupplierSerializer(obj).data)

    def put(self, request, pk):
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        ser = SupplierSerializer(obj, data=request.data)
        ser.is_valid(raise_exception=True)
        instance = ser.save()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='Supplier',
            entity_id=str(instance.id),
            description=f'Supplier updated: {instance.name}',
        )
        return Response(SupplierSerializer(instance).data)

    def delete(self, request, pk):
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        name = obj.name
        obj.delete()
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='DELETE',
            entity_type='Supplier',
            entity_id=str(pk),
            description=f'Supplier deleted: {name}',
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class SupplierTemplateView(APIView):
    def get(self, request):
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Vendors'
        ws.append(['name', 'contact_name', 'phone', 'email', 'address'])
        ws.append(['PT Contoh Supplier', 'Budi Santoso', '0812-3456-7890', 'budi@example.com', 'Jl. Contoh No. 1'])
        ws.append(['CV Bahan Kecantikan', 'Sari Dewi', '0811-9876-5432', '', 'Jl. Raya Kosmetik 5'])
        for col, width in zip('ABCDE', [30, 22, 20, 28, 36]):
            ws.column_dimensions[col].width = width
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': 'attachment; filename="vendors_template.xlsx"'},
        )


class SupplierImportView(APIView):
    def post(self, request):
        f = request.FILES.get('file')
        if not f:
            return Response({'error': 'No file provided.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            wb = openpyxl.load_workbook(io.BytesIO(f.read()), data_only=True)
            ws = wb.active
        except Exception:
            return Response({'error': 'Could not read Excel file.'}, status=status.HTTP_400_BAD_REQUEST)

        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return Response({'error': 'File is empty.'}, status=status.HTTP_400_BAD_REQUEST)

        headers = [str(h).strip().lower() if h is not None else '' for h in rows[0]]
        if 'name' not in headers:
            return Response({'error': 'Column "name" is required in the first row.'}, status=status.HTTP_400_BAD_REQUEST)

        def col(row, field):
            try:
                idx = headers.index(field)
                val = row[idx] if idx < len(row) else None
                return str(val).strip() if val is not None else ''
            except ValueError:
                return ''

        created = updated = 0
        errors = []
        for i, row in enumerate(rows[1:], start=2):
            name = col(row, 'name')
            if not name:
                continue
            data = {
                'name': name,
                'contact_name': col(row, 'contact_name'),
                'phone': col(row, 'phone'),
                'email': col(row, 'email'),
                'address': col(row, 'address'),
            }
            existing = Supplier.objects.filter(name__iexact=name).first()
            if existing:
                ser = SupplierSerializer(existing, data=data)
            else:
                ser = SupplierSerializer(data=data)
            if ser.is_valid():
                ser.save()
                if existing:
                    updated += 1
                else:
                    created += 1
            else:
                errors.append({'row': i, 'name': name, 'error': str(ser.errors)})

        return Response({'created': created, 'updated': updated, 'errors': errors})


# ── Purchase Invoices ──────────────────────────────────────────────────────────

class PurchaseInvoiceListCreateView(APIView):
    """
    GET  /api/accounting/purchases/
         ?status=unpaid|partial|paid
         ?supplier=<id>
         ?q=<search internal_id or external_invoice_no>
         ?overdue=true   (due_date < today and not paid)

    POST /api/accounting/purchases/
         { external_invoice_no, supplier, payment_account, purchase_date, due_date?,
           notes?, items: [{item?, item_name, quantity, unit, unit_cost}] }
    """

    def get(self, request):
        qs = PurchaseInvoice.objects.select_related('supplier', 'payment_account')

        q = request.query_params.get('q', '').strip()
        if q:
            qs = qs.filter(
                Q(internal_id__icontains=q) |
                Q(external_invoice_no__icontains=q) |
                Q(supplier__name__icontains=q)
            )
        st = request.query_params.get('status', '').strip()
        if st in ('unpaid', 'partial', 'paid'):
            qs = qs.filter(status=st)
        supplier_id = request.query_params.get('supplier', '').strip()
        if supplier_id:
            qs = qs.filter(supplier_id=supplier_id)
        overdue = request.query_params.get('overdue', '').strip()
        if overdue == 'true':
            today = timezone.now().date()
            qs = qs.filter(due_date__lt=today).exclude(status='paid')

        # Hide voided invoices unless the caller explicitly asks to include them.
        if request.query_params.get('include_voided', '').strip().lower() not in ('1', 'true', 'yes'):
            qs = qs.filter(is_voided=False)

        return Response(PurchaseInvoiceListSerializer(qs, many=True).data)

    def post(self, request):
        data = request.data

        # Support multipart (with image) or plain JSON
        items_raw = data.get('items', [])
        if isinstance(items_raw, str):
            try:
                items = json.loads(items_raw)
            except (json.JSONDecodeError, ValueError):
                return Response({'error': 'Invalid items JSON.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            items = items_raw

        additional_costs_raw = data.get('additional_costs', [])
        if isinstance(additional_costs_raw, str):
            try:
                additional_costs = json.loads(additional_costs_raw)
            except (json.JSONDecodeError, ValueError):
                return Response({'error': 'Invalid additional_costs JSON.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            additional_costs = additional_costs_raw

        if not items:
            return Response({'error': 'At least one item is required.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            supplier     = Supplier.objects.get(pk=data['supplier'])
            payment_acct = ChartOfAccounts.objects.get(pk=data['payment_account'], account_type='asset')
        except (Supplier.DoesNotExist, ChartOfAccounts.DoesNotExist, KeyError):
            return Response({'error': 'Invalid supplier or payment account.'}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve optional invoice-level warehouse
        warehouse_id = data.get('warehouse') or None
        warehouse_obj = None
        if warehouse_id:
            try:
                warehouse_obj = Warehouse.objects.get(pk=warehouse_id)
            except Warehouse.DoesNotExist:
                return Response({'error': 'Invalid warehouse.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            invoice = PurchaseInvoice.objects.create(
                external_invoice_no=data.get('external_invoice_no', ''),
                supplier=supplier,
                payment_account=payment_acct,
                warehouse=warehouse_obj,
                purchase_date=data['purchase_date'],
                due_date=data.get('due_date') or None,
                notes=data.get('notes', ''),
                created_by=_actor(request),
            )

            # Attach image if uploaded
            image_file = request.FILES.get('invoice_image')
            if image_file:
                invoice.invoice_image = image_file
                invoice.save(update_fields=['invoice_image'])

            # ── Step 1: parse items and compute per-item adjusted subtotals ──────
            parsed = []
            items_subtotal = Decimal('0')
            total_units = Decimal('0')

            for row in items:
                qty            = _safe_decimal(row.get('quantity', 0))
                cost           = _safe_decimal(row.get('unit_cost', 0))
                total_discount = _safe_decimal(row.get('total_discount', 0))
                line_type      = row.get('line_type', 'stock')
                item_id        = row.get('item') or None
                expense_acct_id = row.get('expense_account') or None

                # Row-level warehouse falls back to invoice-level warehouse
                row_wh_id = row.get('warehouse') or warehouse_id or None

                gross          = qty * cost
                # Ensure discount does not exceed gross
                discount_capped = min(total_discount, gross)
                adjusted_sub   = gross - discount_capped

                items_subtotal += adjusted_sub
                if line_type == 'stock' and qty > 0:
                    total_units += qty

                parsed.append({
                    'line_type':       line_type,
                    'item_id':         item_id,
                    'item_name':       row.get('item_name', ''),
                    'quantity':        qty,
                    'unit':            row.get('unit', ''),
                    'unit_cost':       cost,
                    'total_discount':  discount_capped,
                    'adjusted_sub':    adjusted_sub,
                    'expense_acct_id': expense_acct_id,
                    'warehouse_id':    row_wh_id,
                })

            # ── Step 2: compute net adjustment from additional costs ─────────────
            running_total = items_subtotal
            net_adjustment = Decimal('0')

            cost_objs = []
            for i, ac in enumerate(additional_costs):
                name        = str(ac.get('name', '')).strip()
                modifier    = ac.get('modifier', 'add')
                amount_type = ac.get('amount_type', 'cash')
                amount      = _safe_decimal(ac.get('amount', 0))

                if not name or amount <= 0:
                    continue

                if amount_type == 'percent':
                    adj = running_total * amount / Decimal('100')
                else:
                    adj = amount

                if modifier == 'subtract':
                    adj = -adj

                running_total  += adj
                net_adjustment += adj

                cost_objs.append(PurchaseAdditionalCost(
                    invoice=invoice,
                    name=name,
                    modifier=modifier,
                    amount_type=amount_type,
                    amount=amount,
                    sort_order=i,
                ))

            if cost_objs:
                PurchaseAdditionalCost.objects.bulk_create(cost_objs)

            grand_total = items_subtotal + net_adjustment

            # ── Step 3: distribute net_adjustment equally per stock unit ─────────
            per_unit_adj = Decimal('0')
            if total_units > 0:
                per_unit_adj = net_adjustment / total_units

            # ── Step 4: create item records and batches ───────────────────────────
            item_objs = []
            batches_to_create = []

            for p in parsed:
                qty = p['quantity']
                if qty > 0:
                    base_unit_cost = p['adjusted_sub'] / qty
                    actual = base_unit_cost + (per_unit_adj if p['line_type'] == 'stock' else Decimal('0'))
                else:
                    base_unit_cost = p['unit_cost']
                    actual = p['unit_cost']

                item_objs.append(PurchaseInvoiceItem(
                    invoice=invoice,
                    line_type=p['line_type'],
                    item_id=p['item_id'],
                    item_name=p['item_name'],
                    quantity=qty,
                    unit=p['unit'],
                    unit_cost=p['unit_cost'],
                    total_discount=p['total_discount'],
                    actual_unit_cost=actual,
                    expense_account_id=p['expense_acct_id'],
                    warehouse_id=p['warehouse_id'],
                ))

                if p['line_type'] == 'stock' and p['item_id'] and p['warehouse_id'] and qty > 0:
                    batches_to_create.append({
                        'item_id':     p['item_id'],
                        'warehouse_id': p['warehouse_id'],
                        'qty':         qty,
                        'cost':        actual,
                    })

            PurchaseInvoiceItem.objects.bulk_create(item_objs)

            # Create inventory batches (FIFO stock-in) with actual_unit_cost as batch value
            for b in batches_to_create:
                InventoryBatch.objects.create(
                    item_id=b['item_id'],
                    warehouse_id=b['warehouse_id'],
                    input_date=invoice.purchase_date,
                    quantity_initial=b['qty'],
                    quantity_remaining=b['qty'],
                    value=b['cost'],
                    purchase_invoice=invoice,
                )

            invoice.total_amount = grand_total
            invoice.save(update_fields=['total_amount'])

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='CREATE',
                entity_type='PurchaseInvoice',
                entity_id=str(invoice.id),
                description=f'Purchase invoice created: {invoice.internal_id}',
            )

        return Response(
            PurchaseInvoiceDetailSerializer(invoice, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )


class PurchaseInvoiceDetailView(APIView):
    def _get(self, pk):
        try:
            return PurchaseInvoice.objects.select_related(
                'supplier', 'payment_account', 'created_by', 'warehouse',
            ).prefetch_related('items__item', 'items__expense_account', 'additional_costs').get(pk=pk)
        except PurchaseInvoice.DoesNotExist:
            return None

    def get(self, request, pk):
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(PurchaseInvoiceDetailSerializer(obj, context={'request': request}).data)

    def patch(self, request, pk):
        """Update metadata only (notes, dates, invoice number). Items require PUT."""
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if obj.is_voided:
            return Response(
                {'error': 'Cannot edit a voided invoice.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        updatable = ['external_invoice_no', 'notes', 'due_date', 'purchase_date']
        for field in updatable:
            if field in request.data:
                setattr(obj, field, request.data[field] or (None if field == 'due_date' else ''))
        obj.save(update_fields=[f for f in updatable if f in request.data])

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='PurchaseInvoice',
            entity_id=str(obj.id),
            description=f'Purchase invoice metadata updated: {obj.internal_id}',
        )
        return Response(PurchaseInvoiceDetailSerializer(obj, context={'request': request}).data)

    @transaction.atomic
    def put(self, request, pk):
        """Full replacement of invoice items, costs, and header fields. Only allowed when amount_paid == 0."""
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if obj.is_voided:
            return Response(
                {'error': 'Cannot edit a voided invoice.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if obj.amount_paid > 0:
            return Response(
                {'error': 'Cannot edit an invoice that has payments recorded.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Block edit if any inventory from this invoice has already been partially consumed
        consumed_batches = obj.inventory_batches.filter(
            quantity_remaining__lt=F('quantity_initial')
        )
        if consumed_batches.exists():
            return Response(
                {'error': 'Cannot edit: some inventory from this invoice has already been used.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = request.data

        items_raw = data.get('items', [])
        if isinstance(items_raw, str):
            try:
                items = json.loads(items_raw)
            except (json.JSONDecodeError, ValueError):
                return Response({'error': 'Invalid items JSON.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            items = items_raw

        additional_costs_raw = data.get('additional_costs', [])
        if isinstance(additional_costs_raw, str):
            try:
                additional_costs = json.loads(additional_costs_raw)
            except (json.JSONDecodeError, ValueError):
                return Response({'error': 'Invalid additional_costs JSON.'}, status=status.HTTP_400_BAD_REQUEST)
        else:
            additional_costs = additional_costs_raw

        if not items:
            return Response({'error': 'At least one item is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Resolve supplier and payment account
        try:
            supplier = Supplier.objects.get(pk=data['supplier'])
            payment_acct = ChartOfAccounts.objects.get(pk=data['payment_account'], account_type='asset')
        except (Supplier.DoesNotExist, ChartOfAccounts.DoesNotExist, KeyError):
            return Response({'error': 'Invalid supplier or payment account.'}, status=status.HTTP_400_BAD_REQUEST)

        warehouse_id = data.get('warehouse') or None
        warehouse_obj = None
        if warehouse_id:
            try:
                warehouse_obj = Warehouse.objects.get(pk=warehouse_id)
            except Warehouse.DoesNotExist:
                return Response({'error': 'Invalid warehouse.'}, status=status.HTTP_400_BAD_REQUEST)

        # Remove old inventory batches and line items
        obj.inventory_batches.all().delete()
        obj.items.all().delete()
        obj.additional_costs.all().delete()

        # Update header fields
        obj.supplier = supplier
        obj.payment_account = payment_acct
        obj.warehouse = warehouse_obj
        obj.purchase_date = data.get('purchase_date', obj.purchase_date)
        obj.due_date = data.get('due_date') or None
        obj.external_invoice_no = data.get('external_invoice_no', obj.external_invoice_no)
        obj.notes = data.get('notes', obj.notes)
        obj.save(update_fields=[
            'supplier', 'payment_account', 'warehouse',
            'purchase_date', 'due_date', 'external_invoice_no', 'notes',
        ])

        # Update invoice image if provided
        image_file = request.FILES.get('invoice_image')
        if image_file:
            obj.invoice_image = image_file
            obj.save(update_fields=['invoice_image'])

        # Parse items and compute subtotals (same logic as POST)
        parsed = []
        items_subtotal = Decimal('0')
        total_units = Decimal('0')

        for row in items:
            qty            = _safe_decimal(row.get('quantity', 0))
            cost           = _safe_decimal(row.get('unit_cost', 0))
            total_discount = _safe_decimal(row.get('total_discount', 0))
            line_type      = row.get('line_type', 'stock')
            item_id        = row.get('item') or None
            expense_acct_id = row.get('expense_account') or None

            row_wh_id = row.get('warehouse') or warehouse_id or None

            gross           = qty * cost
            discount_capped = min(total_discount, gross)
            adjusted_sub    = gross - discount_capped

            items_subtotal += adjusted_sub
            if line_type == 'stock' and qty > 0:
                total_units += qty

            parsed.append({
                'line_type':       line_type,
                'item_id':         item_id,
                'item_name':       row.get('item_name', ''),
                'quantity':        qty,
                'unit':            row.get('unit', ''),
                'unit_cost':       cost,
                'total_discount':  discount_capped,
                'adjusted_sub':    adjusted_sub,
                'expense_acct_id': expense_acct_id,
                'warehouse_id':    row_wh_id,
            })

        # Compute net adjustment from additional costs
        running_total  = items_subtotal
        net_adjustment = Decimal('0')
        cost_objs      = []

        for i, ac in enumerate(additional_costs):
            name        = str(ac.get('name', '')).strip()
            modifier    = ac.get('modifier', 'add')
            amount_type = ac.get('amount_type', 'cash')
            amount      = _safe_decimal(ac.get('amount', 0))

            if not name or amount <= 0:
                continue

            if amount_type == 'percent':
                adj = running_total * amount / Decimal('100')
            else:
                adj = amount

            if modifier == 'subtract':
                adj = -adj

            running_total  += adj
            net_adjustment += adj

            cost_objs.append(PurchaseAdditionalCost(
                invoice=obj,
                name=name,
                modifier=modifier,
                amount_type=amount_type,
                amount=amount,
                sort_order=i,
            ))

        if cost_objs:
            PurchaseAdditionalCost.objects.bulk_create(cost_objs)

        grand_total  = items_subtotal + net_adjustment
        per_unit_adj = Decimal('0')
        if total_units > 0:
            per_unit_adj = net_adjustment / total_units

        # Create new item records and batches
        item_objs         = []
        batches_to_create = []

        for p in parsed:
            qty = p['quantity']
            if qty > 0:
                base_unit_cost = p['adjusted_sub'] / qty
                actual = base_unit_cost + (per_unit_adj if p['line_type'] == 'stock' else Decimal('0'))
            else:
                base_unit_cost = p['unit_cost']
                actual = p['unit_cost']

            item_objs.append(PurchaseInvoiceItem(
                invoice=obj,
                line_type=p['line_type'],
                item_id=p['item_id'],
                item_name=p['item_name'],
                quantity=qty,
                unit=p['unit'],
                unit_cost=p['unit_cost'],
                total_discount=p['total_discount'],
                actual_unit_cost=actual,
                expense_account_id=p['expense_acct_id'],
                warehouse_id=p['warehouse_id'],
            ))

            if p['line_type'] == 'stock' and p['item_id'] and p['warehouse_id'] and qty > 0:
                batches_to_create.append({
                    'item_id':      p['item_id'],
                    'warehouse_id': p['warehouse_id'],
                    'qty':          qty,
                    'cost':         actual,
                })

        PurchaseInvoiceItem.objects.bulk_create(item_objs)

        for b in batches_to_create:
            InventoryBatch.objects.create(
                item_id=b['item_id'],
                warehouse_id=b['warehouse_id'],
                input_date=obj.purchase_date,
                quantity_initial=b['qty'],
                quantity_remaining=b['qty'],
                value=b['cost'],
                purchase_invoice=obj,
            )

        obj.total_amount = grand_total
        obj.save(update_fields=['total_amount'])

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='UPDATE',
            entity_type='PurchaseInvoice',
            entity_id=str(obj.id),
            description=f'Purchase invoice fully updated: {obj.internal_id}',
        )

        obj.refresh_from_db()
        return Response(PurchaseInvoiceDetailSerializer(
            self._get(pk), context={'request': request}
        ).data)

    @transaction.atomic
    def delete(self, request, pk):
        """Void (soft-delete) a purchase invoice: reverse stock and accounting but keep the record."""
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        if obj.is_voided:
            return Response(
                {'error': 'Invoice is already voided.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if obj.amount_paid > 0:
            return Response(
                {'error': 'Cannot void an invoice that has payments recorded.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Block voiding if any inventory from this invoice has already been partially consumed
        consumed_batches = obj.inventory_batches.filter(
            quantity_remaining__lt=F('quantity_initial')
        )
        if consumed_batches.exists():
            return Response(
                {'error': 'Cannot void: some inventory from this invoice has already been used.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today       = timezone.now().date()
        internal_id = obj.internal_id
        journal_entries = []

        # Reversal journal entries — one per expense line with an explicit account
        for line in obj.items.all():
            if line.line_type == 'expense' and line.expense_account_id:
                amt = line.quantity * line.actual_unit_cost
                if amt > 0:
                    journal_entries.append(LedgerEntry(
                        account_id=line.expense_account_id,
                        date=today,
                        description=f'Reversal beban: {line.item_name} — pembatalan {internal_id}',
                        entry_type='credit',
                        amount=amt,
                        source_type='purchase',
                        purchase_invoice=obj,
                    ))

        # One debit entry to the payment account documenting the voided payable
        if obj.total_amount > 0:
            journal_entries.append(LedgerEntry(
                account=obj.payment_account,
                date=today,
                description=f'Pembatalan faktur pembelian {internal_id}',
                entry_type='debit',
                amount=obj.total_amount,
                source_type='purchase',
                purchase_invoice=obj,
            ))

        if journal_entries:
            LedgerEntry.objects.bulk_create(journal_entries)

        # Remove inventory batches created from this invoice (stock never came in)
        obj.inventory_batches.all().delete()

        obj.is_voided = True
        obj.voided_at = timezone.now()
        obj.voided_by = _actor(request)
        obj.save(update_fields=['is_voided', 'voided_at', 'voided_by'])

        AuditLog.objects.create(
            performed_by=_actor(request),
            action='VOID',
            entity_type='PurchaseInvoice',
            entity_id=str(pk),
            description=f'Purchase invoice voided: {internal_id} — inventory reversed, journal updated',
        )
        return Response(PurchaseInvoiceDetailSerializer(obj, context={'request': request}).data)


class PurchaseInvoicePayView(APIView):
    """
    POST /api/accounting/purchases/<pk>/pay/
    Body: { amount }

    Records a payment against the purchase invoice.
    Debits the payment_account balance and creates a LedgerEntry.
    """

    def post(self, request, pk):
        try:
            invoice = PurchaseInvoice.objects.select_related('payment_account').get(pk=pk)
        except PurchaseInvoice.DoesNotExist:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        if invoice.status == 'paid':
            return Response({'error': 'Invoice is already fully paid.'}, status=status.HTTP_400_BAD_REQUEST)

        amount = _safe_decimal(request.data.get('amount', 0))
        if amount <= 0:
            return Response({'error': 'Payment amount must be greater than zero.'}, status=status.HTTP_400_BAD_REQUEST)

        remaining = invoice.total_amount - invoice.amount_paid
        if amount > remaining:
            return Response(
                {'error': f'Payment exceeds remaining balance of Rp {remaining:,.0f}.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            invoice.amount_paid += amount
            invoice.refresh_status()
            invoice.save(update_fields=['amount_paid', 'status'])

            # Credit the payment_account (cash/bank decreases)
            ChartOfAccounts.objects.filter(pk=invoice.payment_account_id).update(
                balance=invoice.payment_account.balance - amount
            )

            LedgerEntry.objects.create(
                account=invoice.payment_account,
                date=timezone.now().date(),
                description=f'Pembayaran pembelian {invoice.internal_id}',
                entry_type='credit',
                amount=amount,
                source_type='purchase',
                purchase_invoice=invoice,
            )

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='UPDATE',
                entity_type='PurchaseInvoice',
                entity_id=str(invoice.id),
                description=f'Payment Rp{amount:,.0f} recorded for {invoice.internal_id}, status → {invoice.status}',
            )

        return Response(PurchaseInvoiceDetailSerializer(invoice, context={'request': request}).data)


class PurchaseLastPriceView(APIView):
    """GET /api/accounting/purchases/last-price/?item=<id>"""

    def get(self, request):
        item_id = request.query_params.get('item', '').strip()
        if not item_id:
            return Response({'error': 'item parameter is required.'}, status=status.HTTP_400_BAD_REQUEST)
        try:
            item_id = int(item_id)
        except ValueError:
            return Response({'error': 'Invalid item id.'}, status=status.HTTP_400_BAD_REQUEST)

        last = (
            PurchaseInvoiceItem.objects
            .filter(item_id=item_id, line_type='stock')
            .select_related('invoice')
            .order_by('-invoice__purchase_date', '-id')
            .first()
        )
        if last:
            return Response({
                'last_price': str(last.unit_cost),
                'last_date':  str(last.invoice.purchase_date),
                'invoice_id': last.invoice.internal_id,
            })
        return Response({'last_price': None, 'last_date': None, 'invoice_id': None})


# ── Account Transfers ──────────────────────────────────────────────────────────

class AccountTransferListCreateView(APIView):
    """
    GET  /api/accounting/transfers/?date_from=&date_to=&account=
    POST /api/accounting/transfers/
         { transfer_date, from_account, to_account, amount, description, reference? }
    """

    def get(self, request):
        qs = AccountTransfer.objects.select_related('from_account', 'to_account', 'created_by')

        date_from = request.query_params.get('date_from', '').strip()
        date_to   = request.query_params.get('date_to', '').strip()
        acct_id   = request.query_params.get('account', '').strip()

        if date_from:
            qs = qs.filter(transfer_date__gte=date_from)
        if date_to:
            qs = qs.filter(transfer_date__lte=date_to)
        if acct_id:
            qs = qs.filter(Q(from_account_id=acct_id) | Q(to_account_id=acct_id))

        return Response(AccountTransferSerializer(qs, many=True).data)

    def post(self, request):
        data = request.data

        try:
            from_acct = ChartOfAccounts.objects.get(pk=data['from_account'])
            to_acct   = ChartOfAccounts.objects.get(pk=data['to_account'])
        except (ChartOfAccounts.DoesNotExist, KeyError):
            return Response({'error': 'Invalid account(s).'}, status=status.HTTP_400_BAD_REQUEST)

        if from_acct.id == to_acct.id:
            return Response({'error': 'Rekening asal dan tujuan tidak boleh sama.'}, status=status.HTTP_400_BAD_REQUEST)

        amount = _safe_decimal(data.get('amount', 0))
        if amount <= 0:
            return Response({'error': 'Jumlah harus lebih dari nol.'}, status=status.HTTP_400_BAD_REQUEST)

        description = (data.get('description') or '').strip()
        if not description:
            return Response({'error': 'Keterangan wajib diisi.'}, status=status.HTTP_400_BAD_REQUEST)

        transfer_date = data.get('transfer_date')
        if not transfer_date:
            return Response({'error': 'Tanggal transfer wajib diisi.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            transfer = AccountTransfer.objects.create(
                transfer_date=transfer_date,
                from_account=from_acct,
                to_account=to_acct,
                amount=amount,
                description=description,
                reference=data.get('reference', ''),
                created_by=_actor(request),
            )

            # Update balances
            ChartOfAccounts.objects.filter(pk=from_acct.pk).update(balance=from_acct.balance - amount)
            ChartOfAccounts.objects.filter(pk=to_acct.pk).update(balance=to_acct.balance + amount)

            # Ledger entries: credit from_account, debit to_account
            LedgerEntry.objects.bulk_create([
                LedgerEntry(
                    account=from_acct,
                    date=transfer_date,
                    description=description,
                    entry_type='credit',
                    amount=amount,
                    source_type='transfer',
                    transfer=transfer,
                ),
                LedgerEntry(
                    account=to_acct,
                    date=transfer_date,
                    description=description,
                    entry_type='debit',
                    amount=amount,
                    source_type='transfer',
                    transfer=transfer,
                ),
            ])

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='CREATE',
                entity_type='AccountTransfer',
                entity_id=str(transfer.id),
                description=f'Transfer Rp{amount:,.0f} dari {from_acct} ke {to_acct}',
            )

        return Response(AccountTransferSerializer(transfer).data, status=status.HTTP_201_CREATED)


class AccountTransferDetailView(APIView):
    def _get(self, pk):
        try:
            return AccountTransfer.objects.select_related('from_account', 'to_account', 'created_by').get(pk=pk)
        except AccountTransfer.DoesNotExist:
            return None

    def get(self, request, pk):
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(AccountTransferSerializer(obj).data)

    def delete(self, request, pk):
        """Reverse a transfer: restore both account balances and remove ledger entries."""
        obj = self._get(pk)
        if not obj:
            return Response({'error': 'Not found.'}, status=status.HTTP_404_NOT_FOUND)

        with transaction.atomic():
            # Restore balances
            ChartOfAccounts.objects.filter(pk=obj.from_account_id).update(balance=obj.from_account.balance + obj.amount)
            ChartOfAccounts.objects.filter(pk=obj.to_account_id).update(balance=obj.to_account.balance - obj.amount)
            # Remove ledger entries for this transfer
            LedgerEntry.objects.filter(transfer=obj).delete()

            desc = str(obj)
            obj.delete()
            AuditLog.objects.create(
                performed_by=_actor(request),
                action='DELETE',
                entity_type='AccountTransfer',
                entity_id=str(pk),
                description=f'Transfer reversed: {desc}',
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


# ── Manual Journal Adjustment ──────────────────────────────────────────────────

class JournalAdjustmentView(APIView):
    """
    POST /api/accounting/adjustments/
    Body: { account, entry_type (debit|credit), amount, date, description }

    Creates a LedgerEntry and updates the account balance.
    """

    def post(self, request):
        data       = request.data
        entry_type = data.get('entry_type', '').strip().lower()
        if entry_type not in ('debit', 'credit'):
            return Response({'error': 'entry_type must be debit or credit.'}, status=status.HTTP_400_BAD_REQUEST)

        amount = _safe_decimal(data.get('amount', 0))
        if amount <= 0:
            return Response({'error': 'Jumlah harus lebih dari nol.'}, status=status.HTTP_400_BAD_REQUEST)

        description = (data.get('description') or '').strip()
        if not description:
            return Response({'error': 'Keterangan wajib diisi.'}, status=status.HTTP_400_BAD_REQUEST)

        adj_date = data.get('date')
        if not adj_date:
            return Response({'error': 'Tanggal wajib diisi.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            account = ChartOfAccounts.objects.get(pk=data['account'])
        except (ChartOfAccounts.DoesNotExist, KeyError):
            return Response({'error': 'Invalid account.'}, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            if entry_type == 'debit':
                ChartOfAccounts.objects.filter(pk=account.pk).update(balance=account.balance + amount)
            else:
                ChartOfAccounts.objects.filter(pk=account.pk).update(balance=account.balance - amount)

            entry = LedgerEntry.objects.create(
                account=account,
                date=adj_date,
                description=description,
                entry_type=entry_type,
                amount=amount,
                source_type='adjustment',
            )

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='CREATE',
                entity_type='LedgerEntry',
                entity_id=str(entry.id),
                description=f'Manual adjustment: {entry_type.upper()} Rp{amount:,.0f} on {account}',
            )

        return Response(LedgerEntrySerializer(entry).data, status=status.HTTP_201_CREATED)


# ── Journal History ────────────────────────────────────────────────────────────

class JournalHistoryView(APIView):
    """
    GET /api/accounting/journal/
    Query params:
      date_from       YYYY-MM-DD
      date_to         YYYY-MM-DD
      account         account id
      account_number  account_number (int)
      account_type    asset|liability|equity|revenue|cogs|expense|other_income|other_expense
      source_type     invoice|purchase|transfer|adjustment|stock|opname|manual
      entry_type      debit|credit
      q               search description
      page            page number (default 1)
      page_size       items per page (default 100, max 500)
    """

    def get(self, request):
        qs = LedgerEntry.objects.select_related('account', 'invoice', 'purchase_invoice', 'transfer')

        date_from    = request.query_params.get('date_from', '').strip()
        date_to      = request.query_params.get('date_to', '').strip()
        acct_id      = request.query_params.get('account', '').strip()
        acct_number  = request.query_params.get('account_number', '').strip()
        acct_type    = request.query_params.get('account_type', '').strip()
        source_type  = request.query_params.get('source_type', '').strip()
        entry_type   = request.query_params.get('entry_type', '').strip().lower()
        q            = request.query_params.get('q', '').strip()

        if date_from:
            qs = qs.filter(date__gte=date_from)
        if date_to:
            qs = qs.filter(date__lte=date_to)
        if acct_id:
            qs = qs.filter(account_id=acct_id)
        if acct_number:
            qs = qs.filter(account__account_number=acct_number)
        if acct_type:
            qs = qs.filter(account__account_type=acct_type)
        if source_type:
            qs = qs.filter(source_type=source_type)
        if entry_type in ('debit', 'credit'):
            qs = qs.filter(entry_type=entry_type)
        if q:
            qs = qs.filter(description__icontains=q)

        totals = qs.aggregate(
            total_debit=Sum('amount', filter=Q(entry_type='debit')),
            total_credit=Sum('amount', filter=Q(entry_type='credit')),
        )

        try:
            page      = max(1, int(request.query_params.get('page', 1)))
            page_size = min(500, max(1, int(request.query_params.get('page_size', 100))))
        except ValueError:
            page, page_size = 1, 100

        total_count = qs.count()
        offset = (page - 1) * page_size
        entries = qs[offset:offset + page_size]

        return Response({
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_debit': str(totals['total_debit'] or 0),
            'total_credit': str(totals['total_credit'] or 0),
            'results': LedgerEntrySerializer(entries, many=True).data,
        })


# ── Accounting Landing ─────────────────────────────────────────────────────────

class AccountingDashboardView(APIView):
    """
    GET /api/accounting/dashboard/
    Returns unpaid/partial purchase invoices and a summary.
    """

    def get(self, request):
        today = timezone.now().date()

        unpaid_qs = PurchaseInvoice.objects.filter(
            status__in=['unpaid', 'partial']
        ).select_related('supplier', 'payment_account').order_by('due_date', '-purchase_date')

        overdue_count = unpaid_qs.filter(due_date__lt=today).count()
        total_unpaid  = unpaid_qs.aggregate(
            s=Sum('total_amount') - Sum('amount_paid')
        )['s'] or Decimal('0')

        return Response({
            'unpaid_invoices': PurchaseInvoiceListSerializer(unpaid_qs, many=True).data,
            'overdue_count':   overdue_count,
            'total_unpaid':    str(total_unpaid),
        })


# ── Payment Plan (Rencana Pembayaran) ──────────────────────────────────────────

INDO_MONTHS = [
    'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
    'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember',
]


def _payment_plan_queryset(date_from, date_to, include_no_due_date=False):
    """Outstanding (unpaid/partial) purchase invoices whose due_date (jatuh tempo)
    falls in the inclusive range, ordered by due date (nulls last).

    When include_no_due_date is True, invoices without a due date are always
    included regardless of the range so that nothing outstanding is missed.
    """
    date_filter = Q(due_date__gte=date_from, due_date__lte=date_to)
    if include_no_due_date:
        date_filter |= Q(due_date__isnull=True)
    return (
        PurchaseInvoice.objects
        .select_related('supplier')
        .filter(status__in=['unpaid', 'partial'])
        .filter(date_filter)
        .order_by(F('due_date').asc(nulls_last=True), 'purchase_date', 'id')
    )


def _parse_plan_include_no_due_date(request):
    """Whether outstanding invoices without a due date should be included."""
    return request.query_params.get('include_no_due_date', '').strip().lower() in ('1', 'true', 'yes')


def _parse_plan_range(request):
    """Returns (date_from, date_to, error_response)."""
    import datetime
    date_from = request.query_params.get('date_from', '').strip()
    date_to   = request.query_params.get('date_to', '').strip()
    if not date_from or not date_to:
        return None, None, Response(
            {'error': 'date_from dan date_to wajib diisi (YYYY-MM-DD).'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        d_from = datetime.date.fromisoformat(date_from)
        d_to   = datetime.date.fromisoformat(date_to)
    except ValueError:
        return None, None, Response(
            {'error': 'Format tanggal tidak valid. Gunakan YYYY-MM-DD.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if d_from > d_to:
        return None, None, Response(
            {'error': 'date_from tidak boleh setelah date_to.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return d_from, d_to, None


def _default_plan_title(d_from, d_to):
    """e.g. 'Rencana Pembayaran ( 1 - 15 Juni ) 2026'."""
    if d_from.month == d_to.month and d_from.year == d_to.year:
        period = f'( {d_from.day} - {d_to.day} {INDO_MONTHS[d_to.month - 1]} ) {d_to.year}'
    elif d_from.year == d_to.year:
        period = (f'( {d_from.day} {INDO_MONTHS[d_from.month - 1]} - '
                  f'{d_to.day} {INDO_MONTHS[d_to.month - 1]} ) {d_to.year}')
    else:
        period = (f'( {d_from.day} {INDO_MONTHS[d_from.month - 1]} {d_from.year} - '
                  f'{d_to.day} {INDO_MONTHS[d_to.month - 1]} {d_to.year} )')
    return f'Rencana Pembayaran {period}'


class PaymentPlanPreviewView(APIView):
    """
    GET /api/accounting/payment-plan/preview/?date_from=YYYY-MM-DD&date_to=YYYY-MM-DD
                                             &include_no_due_date=true
    Lists outstanding purchase invoices (unpaid/partial) whose due date falls in
    the range for review before generating the Excel file.
    """

    def get(self, request):
        d_from, d_to, err = _parse_plan_range(request)
        if err:
            return err
        include_no_dd = _parse_plan_include_no_due_date(request)

        rows = []
        total = Decimal('0')
        for inv in _payment_plan_queryset(d_from, d_to, include_no_dd):
            balance = inv.total_amount - inv.amount_paid
            total += balance
            rows.append({
                'id':                 inv.id,
                'internal_id':        inv.internal_id,
                'external_invoice_no': inv.external_invoice_no,
                'supplier_name':      inv.supplier.name,
                'balance_due':        str(balance),
                'due_date':           inv.due_date.isoformat() if inv.due_date else None,
                'purchase_date':      inv.purchase_date.isoformat(),
                'status':             inv.status,
            })

        return Response({
            'date_from':     d_from.isoformat(),
            'date_to':       d_to.isoformat(),
            'default_title': _default_plan_title(d_from, d_to),
            'count':         len(rows),
            'total':         str(total),
            'results':       rows,
        })


class PaymentPlanExportView(APIView):
    """
    GET /api/accounting/payment-plan/export/?date_from=&date_to=&title=
                                            &include_no_due_date=true
    Streams an .xlsx file styled to match the clinic's 'Rencana Pembayaran' template.
    Filters outstanding invoices by due date (jatuh tempo).
    """

    def get(self, request):
        from openpyxl.styles import Alignment, Border, Color, Font, PatternFill, Side

        d_from, d_to, err = _parse_plan_range(request)
        if err:
            return err
        include_no_dd = _parse_plan_include_no_due_date(request)

        title = (request.query_params.get('title', '') or '').strip()
        if not title:
            title = _default_plan_title(d_from, d_to)

        # Period label reused for the credit-card block title.
        if title.lower().startswith('rencana pembayaran'):
            period_label = title[len('rencana pembayaran'):].strip()
        else:
            period_label = _default_plan_title(d_from, d_to)[len('Rencana Pembayaran'):].strip()

        # ── Style primitives (mirrors the reference workbook) ──────────────────
        ACCT_FMT  = '_-* #,##0_-;\\-* #,##0_-;_-* "-"_-;_-@_-'
        DATE_FMT  = '[$-409]d\\-mmm;@'
        thin      = Side(style='thin')
        all_thin  = Border(left=thin, right=thin, top=thin, bottom=thin)
        header_fill = PatternFill(
            patternType='solid',
            fgColor=Color(theme=8, tint=0.7999816888943144),
        )
        f_normal = Font(name='Calibri', size=11)
        f_bold   = Font(name='Calibri', size=11, bold=True)
        f_title  = Font(name='Calibri', size=14, bold=True)
        center   = Alignment(horizontal='center', vertical='center')
        center_v = Alignment(vertical='center')
        left_v   = Alignment(horizontal='left', vertical='center')

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Rencana Pembayaran'

        # Column widths copied from the reference file
        widths = {'A': 33.28515625, 'B': 31.42578125, 'C': 13.28515625,
                  'D': 10.42578125, 'E': 14.5703125, 'F': 18.28515625, 'G': 39.28515625}
        for col, w in widths.items():
            ws.column_dimensions[col].width = w

        # ── Title (merged A1:G1) ───────────────────────────────────────────────
        ws.merge_cells('A1:G1')
        t = ws['A1']
        t.value = title
        t.font = f_title
        t.alignment = Alignment(horizontal='center')
        ws.row_dimensions[1].height = 18.75

        # ── Header row (row 3) ─────────────────────────────────────────────────
        headers = ['No. Invoice', 'Supplier', 'Tagihan', 'DD', 'Tgl Byr', 'No. Rek', 'a/n']
        for idx, label in enumerate(headers):
            c = ws.cell(row=3, column=idx + 1, value=label)
            c.font = f_bold
            c.alignment = center
            c.border = all_thin
            c.fill = header_fill

        # ── Data rows ──────────────────────────────────────────────────────────
        row = 4
        first_data_row = row
        for inv in _payment_plan_queryset(d_from, d_to, include_no_dd):
            balance = inv.total_amount - inv.amount_paid

            a = ws.cell(row=row, column=1, value=inv.external_invoice_no or '')
            a.font = f_normal; a.alignment = left_v; a.border = all_thin

            b = ws.cell(row=row, column=2, value=inv.supplier.name)
            b.font = f_normal; b.alignment = center_v; b.border = all_thin

            c = ws.cell(row=row, column=3, value=float(balance))
            c.font = f_normal; c.alignment = center_v
            c.number_format = ACCT_FMT; c.border = all_thin

            d = ws.cell(row=row, column=4)
            if inv.due_date:
                d.value = inv.due_date
                d.number_format = DATE_FMT
            d.font = f_normal; d.alignment = center; d.border = all_thin

            # E (Tgl Byr), F (No. Rek), G (a/n) — left blank for manual entry
            e = ws.cell(row=row, column=5)
            e.font = f_normal; e.alignment = center; e.border = all_thin
            fcell = ws.cell(row=row, column=6)
            fcell.font = f_normal; fcell.alignment = center; fcell.border = all_thin
            g = ws.cell(row=row, column=7)
            g.font = f_normal; g.alignment = left_v; g.border = all_thin

            row += 1

        last_data_row = row - 1
        if last_data_row < first_data_row:
            last_data_row = first_data_row  # keep SUM range valid even when empty

        # ── Total row (one blank row below the table) ──────────────────────────
        total_row = row + 1
        total_border = Border(top=thin, bottom=Side(style='double'))
        tb = ws.cell(row=total_row, column=2, value='Total')
        tb.font = f_bold; tb.border = total_border
        tc = ws.cell(row=total_row, column=3, value=f'=SUM(C{first_data_row}:C{last_data_row})')
        tc.font = f_bold; tc.number_format = ACCT_FMT; tc.border = total_border

        # ── Static credit-card block ───────────────────────────────────────────
        cc_title_row = total_row + 4
        ws.merge_cells(start_row=cc_title_row, start_column=2, end_row=cc_title_row, end_column=4)
        cc = ws.cell(row=cc_title_row, column=2, value=f'Pembayaran Kartu kredit {period_label}')
        cc.font = f_bold; cc.alignment = Alignment(horizontal='center')
        for col in (2, 3, 4):
            ws.cell(row=cc_title_row, column=col).border = all_thin

        # Spacer row (bordered, empty)
        spacer_row = cc_title_row + 1
        for col in (2, 3, 4):
            ws.cell(row=spacer_row, column=col).border = all_thin

        # Column headers
        hdr_row = spacer_row + 1
        for col, label in ((2, 'BANK'), (3, 'TGL BAYAR'), (4, 'TGL LUNAS')):
            hc = ws.cell(row=hdr_row, column=col, value=label)
            hc.font = f_normal; hc.border = all_thin

        # Static bank rows (TGL LUNAS left blank for manual entry)
        bank_rows = [('CITIBANK', '10-15'), ('MANDIRI', '17-20'),
                     ('BCA', '15-17'), ('BNI', '7-9')]
        r = hdr_row + 1
        for bank, tgl in bank_rows:
            ws.cell(row=r, column=2, value=bank).font = f_normal
            ws.cell(row=r, column=3, value=tgl).font = f_normal
            for col in (2, 3, 4):
                ws.cell(row=r, column=col).border = all_thin
            r += 1

        # ── Page setup (landscape A4, matching the reference) ──────────────────
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.paperSize = 9
        ws.page_margins.left = 0.59
        ws.page_margins.right = 0
        ws.page_margins.top = 0
        ws.page_margins.bottom = 0

        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)

        safe_name = title.replace('/', '-').replace('\\', '-')
        return HttpResponse(
            buf.read(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            headers={'Content-Disposition': f'attachment; filename="{safe_name}.xlsx"'},
        )


# ── Daily Sales ────────────────────────────────────────────────────────────────

class DailySalesView(APIView):
    """
    GET /api/accounting/daily-sales/?date=YYYY-MM-DD
    Returns total sales and breakdown per cash (payment) account for the given date.
    Defaults to today in Jakarta time (WIB, UTC+7).
    """

    def get(self, request):
        import datetime
        from zoneinfo import ZoneInfo

        WIB = ZoneInfo('Asia/Jakarta')
        date_str = request.query_params.get('date', '').strip()
        if date_str:
            try:
                target_date = datetime.date.fromisoformat(date_str)
            except ValueError:
                return Response(
                    {'error': 'Invalid date. Use YYYY-MM-DD.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            target_date = timezone.now().astimezone(WIB).date()

        # Filter invoices for the target date (database stores UTC; compare naive date in local time)
        day_start = datetime.datetime(target_date.year, target_date.month, target_date.day,
                                      tzinfo=WIB)
        day_end   = day_start + datetime.timedelta(days=1)

        invoices = (
            Invoice.objects
            .filter(datetime__gte=day_start, datetime__lt=day_end)
            .select_related('payment_method')
        )

        grand_total = Decimal('0')
        by_account: dict = {}
        total_count = 0

        for inv in invoices:
            grand_total += inv.grand_total
            total_count += 1
            pm = inv.payment_method
            key = pm.id if pm else 0
            if key not in by_account:
                by_account[key] = {
                    'account_id':     pm.id             if pm else None,
                    'account_number': pm.account_number if pm else None,
                    'account_name':   pm.name           if pm else 'Tidak Diketahui',
                    'total':          Decimal('0'),
                    'invoice_count':  0,
                }
            by_account[key]['total']         += inv.grand_total
            by_account[key]['invoice_count'] += 1

        breakdown = sorted(by_account.values(), key=lambda x: -x['total'])
        for row in breakdown:
            row['total'] = str(row['total'])

        return Response({
            'date':          str(target_date),
            'total':         str(grand_total),
            'invoice_count': total_count,
            'by_account':    breakdown,
        })
