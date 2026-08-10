from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import BillingPatientSerializer, todays_notes_by_visit
from ..models import ActivePatient, AppUser, AuditLog, ChartOfAccounts, Invoice, InvoiceItem, PaymentMethod
from ..services.cash_accounts import cash_bank_account_ids


def _already_invoiced(active_patient, lines):
    """Return an existing invoice that already bills every line in ``lines``.

    The POS creates the invoice itself and is meant to pass ``?skip_invoice=1``
    when clearing the queue. When a caller forgets, this endpoint used to bill the
    same visit a second time, producing an invoice with no payment method that
    double-counted the revenue. A query parameter is too easy to omit to be the
    only defence, so the visit's own data is checked as well.

    Matching is by (name, price) against non-voided invoices for the same patient
    on the same day. Guests have no patient record to match on, so they are not
    guarded here — an unmatched guest falls through and is billed normally.
    """
    if not active_patient.patient_no_id or not lines:
        return None

    wanted = {
        ((catalog_item.name if catalog_item else name).strip().lower(), price)
        for catalog_item, name, price, _treatment in lines
    }

    same_day = (
        Invoice.objects
        .filter(patient_no_id=active_patient.patient_no_id,
                datetime__date=timezone.now().date(),
                is_voided=False)
        .prefetch_related('items__item')
    )
    for invoice in same_day:
        billed = {
            (((item.item.name if item.item_id else item.item_name) or '').strip().lower(),
             item.price)
            for item in invoice.items.all()
        }
        if wanted <= billed:
            return invoice
    return None


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
        patients = list(
            ActivePatient.objects
            .filter(status=5)
            .select_related('patient_no', 'medrec')
            .prefetch_related(
                'treatmentsession_set__treatments',
                'treatmentsession_set__beautician',
            )
            .order_by('visit_time')
        )
        # Today's notes for the whole queue in a single query. Letting the
        # serializer resolve them per patient would be an N+1 on an endpoint the
        # POS polls; see todays_notes_by_visit().
        serializer = BillingPatientSerializer(
            patients, many=True,
            context={'notes_by_visit': todays_notes_by_visit(patients)},
        )
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
                'treatments__materials__item',
            )
            .all()
        )

        lines = []  # list of (catalog_item_or_None, item_name, price, treatment)
        for session in sessions:
            for treatment in session.treatments.all():
                catalog_item = getattr(treatment, 'catalog_item', None)
                lines.append((catalog_item, treatment.name, treatment.price, treatment))

        # ── Refuse to bill a visit that was already invoiced ──────────────────
        existing = _already_invoiced(active_patient, lines)
        if existing is not None:
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
                description=(
                    f'Billing queue entry cleared for {label} — treatments already '
                    f'billed on {existing.invoice_number}; duplicate invoice not created'
                ),
            )
            active_patient.delete()
            return Response(
                {'invoice_number': existing.invoice_number, 'already_invoiced': True},
                status=status.HTTP_200_OK,
            )

        # ── Payment method / account ──────────────────────────────────────────
        # At least one of payment_method_id / payment_account_id is required.
        # This endpoint used to accept a checkout with no method at all, which
        # left the cash side of the journal with nowhere to go but the
        # 1100011 clearing account — asserting a receipt nobody could later
        # explain. Validated before anything is written so a rejected checkout
        # leaves the queue entry untouched and the cashier can retry.
        #
        # BillingPage.tsx replaced its PaymentMethod <select> with
        # CashAccountPicker (design doc §3) and now only ever sends
        # payment_account_id — so payment_method_id must be optional here, the
        # same as InvoiceCreateView, or every checkout 400s.
        method_id = request.data.get('payment_method_id')
        payment_account_id = request.data.get('payment_account_id')
        if not method_id and not payment_account_id:
            return Response(
                {'payment_account_id': 'A payment account is required to complete billing.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        payment_method_obj = None
        if method_id:
            payment_method_obj = PaymentMethod.objects.filter(
                pk=method_id, is_active=True,
            ).first()
            if payment_method_obj is None:
                return Response(
                    {'payment_method_id': 'Payment method not found or inactive.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # ── Payment account (design doc §3) ────────────────────────────────────
        if payment_account_id:
            if payment_account_id not in cash_bank_account_ids():
                return Response(
                    {'payment_account_id': ['Not a cash/bank account.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            payment_account_obj = ChartOfAccounts.objects.filter(pk=payment_account_id).first()
            if payment_account_obj is None:
                return Response(
                    {'payment_account_id': ['Not a cash/bank account.']},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            # Not sent — resolved from the payment method, same as
            # InvoiceCreateView, so a checkout is never write-only-legacy.
            payment_account_obj = payment_method_obj.linked_account

        # ── Totals ────────────────────────────────────────────────────────────
        subtotal = sum(price for _, _, price, _ in lines)
        discount = _safe_decimal(request.data.get('discount', 0))
        grand_total = max(subtotal - discount, Decimal('0'))
        promotion_code = (request.data.get('promotion_code') or '').strip()

        # ── Create Invoice ────────────────────────────────────────────────────
        invoice = Invoice.objects.create(
            datetime=timezone.now(),
            patient_no=active_patient.patient_no,
            payment_method=payment_method_obj,
            payment_account=payment_account_obj,
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

        # ── Accounting + material stock deduction ─────────────────────────────
        # Phase 2: journal posting is deferred, same as the POS-created path in
        # invoice_page.py. The invoice is left posting_status='unposted' (model
        # default); a journal run (POST /api/accounting/journal/run/) rebuilds
        # its lines from these InvoiceItem rows and calls the same
        # _post_accounting() used for POS invoices — which already knows how to
        # walk item.treatment.materials for service lines like these — so
        # billing-queue invoices post identically to POS ones, just later.

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
