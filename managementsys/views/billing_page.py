from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from ..api.serializers import BillingPatientSerializer, todays_notes_by_visit
from ..models import (
    EXCLUDED, ActivePatient, AppUser, AuditLog, ChartOfAccounts, Invoice, InvoiceItem,
    PatientNote, PaymentMethod,
)
from ..services.branches import filter_by_branch, write_branch
from ..services.cash_accounts import cash_bank_account_ids
from .patient_page import _release_beauticians


def _already_invoiced(active_patient, lines, days=None):
    """Return an existing invoice that already bills every line in ``lines``.

    The POS creates the invoice itself and is meant to pass ``?skip_invoice=1``
    when clearing the queue. When a caller forgets, this endpoint used to bill the
    same visit a second time, producing an invoice with no payment method that
    double-counted the revenue. A query parameter is too easy to omit to be the
    only defence, so the visit's own data is checked as well.

    Matching is by (name, price) against non-voided invoices for the same patient
    on any of ``days`` (default: today). A carried-over visit passes its arrival
    day as well, since that is when a till would have billed it. Guests have no
    patient record to match on, so they are not guarded here — an unmatched
    guest falls through and is billed normally.
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
                datetime__date__in=days or {timezone.now().date()},
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


def _visit_label(active_patient):
    return (
        active_patient.patient_no.name
        if active_patient.patient_no_id
        else active_patient.guest_name
    )


def _visit_lines(active_patient):
    """Every treatment across the visit's sessions, as (catalog_item, name, price, treatment)."""
    sessions = (
        active_patient.treatmentsession_set
        .prefetch_related(
            'treatments__catalog_item__item_category__revenue_account',
        )
        .all()
    )
    lines = []
    for session in sessions:
        for treatment in session.treatments.all():
            catalog_item = getattr(treatment, 'catalog_item', None)
            lines.append((catalog_item, treatment.name, treatment.price, treatment))
    return lines


def _resolve_payment(data):
    """``(method, account, error_response)`` from payment_method_id / payment_account_id.

    Neither sent gives ``(None, None, None)``; whether that is acceptable is the
    caller's call.
    """
    method_id = data.get('payment_method_id')
    payment_account_id = data.get('payment_account_id')

    payment_method_obj = None
    if method_id:
        payment_method_obj = PaymentMethod.objects.filter(
            pk=method_id, is_active=True,
        ).first()
        if payment_method_obj is None:
            return None, None, Response(
                {'payment_method_id': 'Payment method not found or inactive.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # ── Payment account (design doc §3) ────────────────────────────────────
    if payment_account_id:
        if payment_account_id not in cash_bank_account_ids():
            return None, None, Response(
                {'payment_account_id': ['Not a cash/bank account.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payment_account_obj = ChartOfAccounts.objects.filter(pk=payment_account_id).first()
        if payment_account_obj is None:
            return None, None, Response(
                {'payment_account_id': ['Not a cash/bank account.']},
                status=status.HTTP_400_BAD_REQUEST,
            )
    elif payment_method_obj is not None:
        # Not sent — resolved from the payment method, same as
        # InvoiceCreateView, so a checkout is never write-only-legacy.
        payment_account_obj = payment_method_obj.linked_account
    else:
        payment_account_obj = None
    return payment_method_obj, payment_account_obj, None


def _create_visit_invoice(request, active_patient, lines, *, payment_method=None,
                          payment_account=None, discount=Decimal('0'),
                          promotion_code='', when=None, posting_status='unposted',
                          notes=''):
    """Invoice a visit's treatment lines. Shared by /billing and mark-paid.

    Journal posting is deferred either way (Phase 2). An 'unposted' invoice is
    picked up by the next journal run, which rebuilds its lines from these
    InvoiceItem rows and calls the same _post_accounting() used for POS
    invoices, so it posts identically to a POS sale, just later. Service lines
    carry no cost on either path — see build_invoice_legs. An 'excluded'
    invoice is never picked up at all — see models.EXCLUDED.
    """
    subtotal = sum(price for _, _, price, _ in lines)
    invoice = Invoice.objects.create(
        datetime=when or timezone.now(),
        patient_no=active_patient.patient_no,
        payment_method=payment_method,
        payment_account=payment_account,
        discount=discount,
        tax=Decimal('0'),
        additional_charges=Decimal('0'),
        grand_total=max(subtotal - discount, Decimal('0')),
        promotion_code=promotion_code,
        notes=notes,
        posting_status=posting_status,
        # The sale belongs where the patient was treated, not where the
        # ledger is being read from — hence the visit's own branch, with the
        # cashier's as the fallback for pre-0113 queue rows.
        branch=active_patient.branch or write_branch(request, locked=True),
    )
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

    # ── CRM refresh ───────────────────────────────────────────────────────
    if active_patient.patient_no_id:
        from .crm_page import refresh_crm_profile
        refresh_crm_profile(active_patient.patient_no)
    return invoice


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
            filter_by_branch(
                ActivePatient.objects.filter(status=5),
                request, locked=True, include_null=False,
            )
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

        lines = _visit_lines(active_patient)
        label = _visit_label(active_patient)

        # ── Refuse to bill a visit that was already invoiced ──────────────────
        existing = _already_invoiced(active_patient, lines)
        if existing is not None:
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
        # BillingPage.tsx sends payment_method_id; the WinUI POS and older
        # clients send payment_account_id — either is enough.
        if not request.data.get('payment_method_id') and not request.data.get('payment_account_id'):
            return Response(
                {'payment_account_id': 'A payment account is required to complete billing.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        payment_method_obj, payment_account_obj, error = _resolve_payment(request.data)
        if error is not None:
            return error

        invoice = _create_visit_invoice(
            request, active_patient, lines,
            payment_method=payment_method_obj,
            payment_account=payment_account_obj,
            discount=_safe_decimal(request.data.get('discount', 0)),
            promotion_code=(request.data.get('promotion_code') or '').strip(),
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


# Who may close a visit from the appointments page. Doctors and beauticians see
# that page too, but settling a visit — and above all keeping its sale out of
# the journal — is a till decision.
MARK_PAID_ROLES = ('superuser', 'manager', 'cashier')


class ActivePatientMarkPaidView(APIView):
    """Close a queue visit as paid, from any status.

    ``POST /api/activepatients/<pk>/mark-paid/``

    For carried-over visits: a patient who paid days ago but whose visit never
    reached status 5, so /billing never offered it. Two modes:

    ``exclude_from_journal: false`` (default) — an ordinary sale. Needs a
    payment method; invoiced now and posted by the next journal run exactly
    like a /billing checkout.

    ``exclude_from_journal: true`` — the sale happened and was already booked by
    hand. The invoice is dated at the visit's arrival (when the money changed
    hands), stamped ``posting_status='excluded'`` and never journaled, so it
    shows in sales history, CRM and the patient's record without being counted
    a second time in the GL. A payment method is optional and informational. A
    visit with no treatments recorded has nothing to invoice; it is closed with
    a patient note rather than a Rp 0 invoice.
    """

    def post(self, request, pk):
        if getattr(request.user, 'role', None) not in MARK_PAID_ROLES:
            return Response(
                {'error': 'Only a cashier or manager can mark a visit as paid.'},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            active_patient = ActivePatient.objects.select_related('patient_no').get(id=pk)
        except ActivePatient.DoesNotExist:
            return Response({'error': 'Visit not found.'}, status=status.HTTP_404_NOT_FOUND)

        exclude = bool(request.data.get('exclude_from_journal', False))
        note = (request.data.get('note') or '').strip()[:300]

        payment_method_obj, payment_account_obj, error = _resolve_payment(request.data)
        if error is not None:
            return error
        if not exclude and payment_method_obj is None and payment_account_obj is None:
            return Response(
                {'payment_method_id': 'Choose how the patient paid.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        lines = _visit_lines(active_patient)
        label = _visit_label(active_patient)
        arrived = active_patient.visit_time or timezone.now()
        days = {timezone.now().date(), arrived.date()}

        with transaction.atomic():
            if active_patient.status == 4:
                _release_beauticians(active_patient)

            existing = _already_invoiced(active_patient, lines, days=days)
            if existing is not None:
                AuditLog.objects.create(
                    performed_by=_actor(request),
                    action='DELETE',
                    entity_type='ActivePatient',
                    entity_id=str(active_patient.id),
                    description=(
                        f'Visit closed for {label} — treatments already billed on '
                        f'{existing.invoice_number}; duplicate invoice not created'
                    ),
                )
                active_patient.delete()
                return Response(
                    {'invoice_number': existing.invoice_number, 'already_invoiced': True},
                    status=status.HTTP_200_OK,
                )

            if exclude and not lines:
                if active_patient.patient_no_id:
                    PatientNote.objects.create(
                        patient_no=active_patient.patient_no,
                        date=arrived.date(),
                        content=(
                            'Visit marked as paid outside the system. No treatments were '
                            'recorded in CPMS, so no invoice was created.'
                            + (f' Note: {note}' if note else '')
                        ),
                        author='System',
                    )
                AuditLog.objects.create(
                    performed_by=_actor(request),
                    action='DELETE',
                    entity_type='ActivePatient',
                    entity_id=str(active_patient.id),
                    description=(
                        f'Visit closed for {label} as paid outside the journal — '
                        f'no treatments recorded, no invoice created'
                    ),
                )
                active_patient.delete()
                return Response({'invoice_number': '', 'no_invoice': True}, status=status.HTTP_200_OK)

            if exclude:
                invoice = _create_visit_invoice(
                    request, active_patient, lines,
                    payment_method=payment_method_obj,
                    payment_account=payment_account_obj,
                    when=arrived,
                    posting_status=EXCLUDED,
                    notes=('Sudah dicatat manual — di luar jurnal.'
                           + (f' {note}' if note else ''))[:500],
                )
                description = (
                    f'Invoice {invoice.invoice_number} created for {label} as paid '
                    f'outside the journal (excluded from posting)'
                )
            else:
                invoice = _create_visit_invoice(
                    request, active_patient, lines,
                    payment_method=payment_method_obj,
                    payment_account=payment_account_obj,
                    notes=note,
                )
                description = (
                    f'Invoice {invoice.invoice_number} created for {label} via '
                    f'mark-as-paid on the appointments page'
                )

            AuditLog.objects.create(
                performed_by=_actor(request),
                action='CREATE',
                entity_type='Invoice',
                entity_id=str(invoice.id),
                description=description,
            )
            active_patient.delete()

        return Response(
            {'invoice_number': invoice.invoice_number, 'posting_status': invoice.posting_status},
            status=status.HTTP_200_OK,
        )
