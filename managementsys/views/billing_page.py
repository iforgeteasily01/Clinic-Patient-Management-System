from decimal import Decimal, InvalidOperation

from django.db.models import F
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import BillingPatientSerializer
from ..models import ActivePatient, AppUser, AuditLog, ChartOfAccounts, Invoice, InvoiceItem, TreatmentMaterial
from .inventory_page import _fifo_deduct_global
from .invoice_page import ACC_CASH, _post_legs, _revenue_legs


def _actor(request):
    return request.user if isinstance(request.user, AppUser) else None


def _safe_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError):
        return Decimal('0')


class BillingQueueView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        patients = (
            ActivePatient.objects
            .filter(status=5)
            .select_related('patient_no', 'medrec')
            .prefetch_related(
                'treatmentsession_set__treatments',
                'treatmentsession_set__beautician',
            )
            .order_by('visit_time')
        )
        serializer = BillingPatientSerializer(patients, many=True)
        return Response(serializer.data)


class BillingCompleteView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def delete(self, request, pk):
        try:
            active_patient = ActivePatient.objects.select_related('patient_no').get(
                id=pk, status=5,
            )
        except ActivePatient.DoesNotExist:
            return Response(
                {'error': 'Billing record not found.'},
                status=status.HTTP_404_NOT_FOUND,
            )

        # When the POS app (Medya-Cashier) calls this endpoint it has already
        # created the invoice via POST /api/invoices/create/.  It passes
        # ?skip_invoice=1 to tell us to just clear the queue entry without
        # creating a duplicate invoice.
        skip_invoice = request.query_params.get('skip_invoice') == '1'

        if skip_invoice:
            label = (
                active_patient.patient_no.name
                if active_patient.patient_no_id
                else active_patient.guest_name
            )
            AuditLog.objects.create(
                performed_by=_actor(request),
                action='DELETE',
                entity_type='ActivePatient',
                entity_id=str(active_patient.id),
                description=f'Billing queue entry cleared for {label} (invoice created by POS)',
            )
            active_patient.delete()
            return Response({'invoice_number': ''}, status=status.HTTP_200_OK)

        # ── Collect treatment lines from all sessions ─────────────────────────
        sessions = (
            active_patient.treatmentsession_set
            .prefetch_related(
                'treatments__catalog_item__item_category__revenue_account',
                'treatments__catalog_item__item_category__cogs_account',
                'treatments__materials__item',
            )
            .all()
        )

        lines = []  # list of (catalog_item_or_None, item_name, price, treatment)
        for session in sessions:
            for treatment in session.treatments.all():
                catalog_item = getattr(treatment, 'catalog_item', None)
                lines.append((catalog_item, treatment.name, treatment.price, treatment))

        # ── Totals ────────────────────────────────────────────────────────────
        subtotal = sum(price for _, _, price, _ in lines)
        discount = _safe_decimal(request.data.get('discount', 0))
        grand_total = max(subtotal - discount, Decimal('0'))
        promotion_code = (request.data.get('promotion_code') or '').strip()

        # ── Create Invoice ────────────────────────────────────────────────────
        # The cash side has to land somewhere or the entry cannot balance, so an
        # unspecified payment method falls back to Cash.
        payment_account = None
        if request.data.get('payment_method_id'):
            payment_account = ChartOfAccounts.objects.filter(
                pk=request.data['payment_method_id'],
                parent__account_number=1100000,
                is_head=False,
            ).first()
        if payment_account is None:
            payment_account = ChartOfAccounts.objects.filter(account_number=ACC_CASH).first()

        invoice = Invoice.objects.create(
            datetime=timezone.now(),
            patient_no=active_patient.patient_no,
            payment_method=payment_account,
            discount=discount,
            tax=Decimal('0'),
            additional_charges=Decimal('0'),
            grand_total=grand_total,
            promotion_code=promotion_code,
        )

        # ── Create InvoiceItems ───────────────────────────────────────────────
        InvoiceItem.objects.bulk_create([
            InvoiceItem(
                invoice=invoice,
                item=catalog_item,
                item_name='' if catalog_item else name,
                quantity=Decimal('1'),
                price=price,
                discount_pct=Decimal('0'),
            )
            for catalog_item, name, price, _treatment in lines
        ])

        # ── Post revenue to GL ────────────────────────────────────────────────
        # Delegated so the billing queue writes the same self-balancing block as
        # the POS path — cash debit, per-line revenue (including lines with no
        # catalog item), and the discount plug — as real LedgerEntry rows.
        # Updating ChartOfAccounts.balance alone, as this used to, left the
        # stored balances drifting away from the journal.
        _post_legs(invoice, _revenue_legs(
            invoice,
            [(catalog_item, name, Decimal('1'), price)
             for catalog_item, name, price, _treatment in lines],
        ))

        # ── Post treatment material COGS to GL ───────────────────────────────
        fallback_cogs = ChartOfAccounts.objects.filter(account_number=5100000).first()
        inventory_asset = ChartOfAccounts.objects.filter(account_number=1300000).first()
        for catalog_item, _name, _price, treatment in lines:
            for material in treatment.materials.all():
                _shortfall, cogs_amount = _fifo_deduct_global(material.item_id, material.quantity_small)
                if cogs_amount <= 0:
                    continue
                cat = getattr(catalog_item, 'item_category', None) if catalog_item else None
                cogs_acct = (
                    cat.cogs_account
                    if cat and getattr(cat, 'cogs_account_id', None)
                    else fallback_cogs
                )
                label = catalog_item.name if catalog_item else treatment.name
                if inventory_asset:
                    _post_legs(invoice, [(
                        inventory_asset, 'credit', cogs_amount,
                        f'Invoice {invoice.invoice_number} – material: '
                        f'{material.item.name} for {label}',
                    )])
                if cogs_acct:
                    _post_legs(invoice, [(
                        cogs_acct, 'debit', cogs_amount,
                        f'Invoice {invoice.invoice_number} – material COGS: '
                        f'{material.item.name} for {label}',
                    )])

        # ── CRM refresh ───────────────────────────────────────────────────────
        if active_patient.patient_no_id:
            from .crm_page import refresh_crm_profile
            refresh_crm_profile(active_patient.patient_no)

        label = (
            active_patient.patient_no.name
            if active_patient.patient_no_id
            else active_patient.guest_name
        )
        AuditLog.objects.create(
            performed_by=_actor(request),
            action='CREATE',
            entity_type='Invoice',
            entity_id=str(invoice.id),
            description=f'Invoice {invoice.invoice_number} created via billing queue for {label}',
        )

        active_patient.delete()
        return Response(
            {'invoice_number': invoice.invoice_number},
            status=status.HTTP_200_OK,
        )
