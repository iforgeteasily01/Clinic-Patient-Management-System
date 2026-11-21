from datetime import datetime, timezone as dt_timezone
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command

from managementsys.models import ChartOfAccounts, Invoice, InvoiceItem, Patient

WHEN = datetime(2026, 6, 20, 3, 0, tzinfo=dt_timezone.utc)


@pytest.fixture
def patient(db):
    return Patient.objects.create(patient_no='T00001', name='Test Patient')


def _invoice(number, patient, payment=None, cashier=None, lines=(('Light Peel', '1', '300000'),),
             total='300000', when=WHEN):
    invoice = Invoice.objects.create(
        invoice_number=number, datetime=when, patient_no=patient,
        payment_method=payment, cashier=cashier, grand_total=Decimal(total))
    for name, qty, price in lines:
        InvoiceItem.objects.create(
            invoice=invoice, item_name=name,
            quantity=Decimal(qty), price=Decimal(price))
    return invoice


def _run(**kwargs):
    out = StringIO()
    call_command('void_duplicate_billing_invoices', stdout=out, **kwargs)
    return out.getvalue()


@pytest.mark.django_db
class TestVoidDuplicateBillingInvoices:
    def test_voids_a_phantom_that_duplicates_a_paid_invoice(
            self, patient, gl_accounts):
        paid = _invoice('INV-20260620-1', patient, payment=gl_accounts['cash_method'])
        phantom = _invoice('INV-20260620-2', patient, payment=gl_accounts['legacy_clearing_method'])

        _run(apply=True)
        paid.refresh_from_db()
        phantom.refresh_from_db()
        assert phantom.is_voided
        assert not paid.is_voided

    def test_never_voids_both_halves_of_a_pair(self, patient, gl_accounts):
        first = _invoice('INV-20260620-1', patient, payment=gl_accounts['cash_method'])
        second = _invoice('INV-20260620-2', patient, payment=gl_accounts['cash_method'])

        _run(apply=True)
        first.refresh_from_db()
        second.refresh_from_db()
        assert not first.is_voided
        assert not second.is_voided

    def test_never_voids_ipos_imports(self, patient, gl_accounts):
        paid = _invoice('INV-20260620-1', patient, payment=gl_accounts['cash_method'])
        imported = _invoice('IPOS-4045/KSR/GD/0626', patient,
                            payment=gl_accounts['legacy_clearing_method'])

        _run(apply=True)
        imported.refresh_from_db()
        assert not imported.is_voided
        assert not paid.is_voided

    def test_one_payment_absorbs_only_one_duplicate(self, patient, gl_accounts):
        _invoice('INV-20260620-1', patient, payment=gl_accounts['cash_method'])
        first = _invoice('INV-20260620-2', patient, payment=gl_accounts['legacy_clearing_method'])
        second = _invoice('INV-20260620-3', patient, payment=gl_accounts['legacy_clearing_method'])

        _run(apply=True)
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.is_voided
        assert not second.is_voided

    def test_keeps_a_phantom_with_no_counterpart(self, patient, gl_accounts):
        orphan = _invoice('INV-20260620-2', patient, payment=gl_accounts['legacy_clearing_method'])

        output = _run(apply=True)
        orphan.refresh_from_db()
        assert not orphan.is_voided
        assert 'genuinely unbilled' in output

    def test_keeps_a_partial_overlap(self, patient, gl_accounts):
        _invoice('INV-20260620-1', patient, payment=gl_accounts['cash_method'])
        partial = _invoice(
            'INV-20260620-2', patient, payment=gl_accounts['legacy_clearing_method'],
            lines=(('Light Peel', '1', '300000'), ('Dermapen', '1', '400000')),
            total='700000')

        output = _run(apply=True)
        partial.refresh_from_db()
        assert not partial.is_voided
        assert 'partial overlap' in output

    def test_does_not_match_across_different_days(self, patient, gl_accounts):
        _invoice('INV-20260620-1', patient, payment=gl_accounts['cash_method'])
        next_day = _invoice(
            'INV-20260621-1', patient, payment=gl_accounts['legacy_clearing_method'],
            when=datetime(2026, 6, 21, 3, 0, tzinfo=dt_timezone.utc))

        _run(apply=True)
        next_day.refresh_from_db()
        assert not next_day.is_voided

    def test_dry_run_writes_nothing(self, patient, gl_accounts):
        _invoice('INV-20260620-1', patient, payment=gl_accounts['cash_method'])
        phantom = _invoice('INV-20260620-2', patient, payment=gl_accounts['legacy_clearing_method'])

        output = _run()
        phantom.refresh_from_db()
        assert not phantom.is_voided
        assert 'DRY RUN' in output


@pytest.mark.django_db
class TestBillingCompleteDuplicateGuard:
    def test_guard_rejects_a_second_invoice_for_the_same_treatments(
            self, auth_api, gl_accounts, patient):
        from managementsys.models import ActivePatient, Beauticians, Treatment, TreatmentSession
        from django.urls import reverse
        from django.utils import timezone

        treatment = Treatment.objects.create(
            code='LP', name='Light Peel', category='Peeling', price=Decimal('300000'))
        active = ActivePatient.objects.create(
            patient_no=patient, status=5, consult_status=False)
        session = TreatmentSession.objects.create(
            active_patient=active, patient_no=patient,
            beautician=Beauticians.objects.create(beautician_name='B'))
        session.treatments.add(treatment)

        _invoice('INV-POS-1', patient, payment=gl_accounts['cash_method'],
                 lines=(('Light Peel', '1', '300000'),), when=timezone.now())

        before = Invoice.objects.count()
        res = auth_api.delete(reverse('billing-complete', args=[active.id]))
        assert res.status_code == 200, res.content
        assert res.json().get('already_invoiced') is True
        assert Invoice.objects.count() == before, 'a duplicate invoice was created'
        assert not ActivePatient.objects.filter(id=active.id).exists()
