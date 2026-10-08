"""Mark-as-paid from the appointments page, and the 'excluded' invoice state.

Carried-over visits were often settled by hand days ago and then booked into
the GL by manual journal. Closing them in CPMS must record the sale for the
patient's history without posting it a second time — so an 'excluded' invoice
has to stay out of every path that writes to the ledger: the sweep, a sales
return, and the void/edit memo.
"""
from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    ActivePatient, AuditLog, Beauticians, Invoice, LedgerEntry, Patient, PatientNote,
    Treatment, TreatmentSession,
)
from managementsys.services.journal_sweep import _gather_events
from managementsys.services.sales_returns import SalesReturnError, validate_invoice

from .factories import AppUserFactory

THREE_DAYS_AGO = timezone.now() - timedelta(days=3)


def _url(pk):
    return f'/api/activepatients/{pk}/mark-paid/'


@pytest.fixture
def patient(db):
    return Patient.objects.create(patient_no='M00001', name='Mira')


@pytest.fixture
def treatment(db):
    return Treatment.objects.create(code='FACIAL-1', name='Facial', category='Facial',
                                    price=Decimal('250000'))


def _visit(patient, status=4, treatments=(), beautician=None, arrived=THREE_DAYS_AGO):
    visit = ActivePatient.objects.create(patient_no=patient, status=status, consult_status=False)
    ActivePatient.objects.filter(pk=visit.pk).update(visit_time=arrived)
    visit.refresh_from_db()
    if treatments or beautician:
        session = TreatmentSession.objects.create(
            active_patient=visit, patient_no=patient, beautician=beautician)
        session.treatments.set(treatments)
    return visit


@pytest.mark.django_db
class TestExcludedFromJournal:
    def test_creates_an_excluded_invoice_dated_at_arrival(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])

        res = auth_api.post(_url(visit.pk), {'exclude_from_journal': True, 'note': 'nota 118'},
                            format='json')

        assert res.status_code == 200, res.content
        invoice = Invoice.objects.get(invoice_number=res.data['invoice_number'])
        assert invoice.posting_status == 'excluded'
        assert invoice.datetime == visit.visit_time
        assert invoice.grand_total == Decimal('250000')
        assert 'nota 118' in invoice.notes
        assert not ActivePatient.objects.filter(pk=visit.pk).exists()

    def test_the_sweep_never_selects_it(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])
        auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')

        events = _gather_events(timezone.now().date())
        swept = [obj for day in events.values() for kind, obj in day if kind == 'invoice']
        assert swept == []

        run = auth_api.post(reverse('accounting-journal-run'),
                            {'date_to': timezone.now().date().isoformat()}, format='json')
        assert run.status_code == 200, run.content
        assert LedgerEntry.objects.count() == 0
        assert Invoice.objects.get().posting_status == 'excluded'

    def test_needs_no_payment_method(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])
        res = auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')
        assert res.status_code == 200

    def test_a_return_against_it_is_refused(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])
        res = auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')
        invoice = Invoice.objects.get(invoice_number=res.data['invoice_number'])

        with pytest.raises(SalesReturnError):
            validate_invoice(invoice)

    def test_voiding_it_writes_no_memo(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])
        res = auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')
        invoice = Invoice.objects.get(invoice_number=res.data['invoice_number'])

        void = auth_api.delete(reverse('invoice-detail', args=[invoice.pk]))

        assert void.status_code == 200, void.content
        invoice.refresh_from_db()
        assert invoice.is_voided
        assert LedgerEntry.objects.count() == 0

    def test_a_visit_with_no_treatments_closes_without_an_invoice(self, auth_api, patient, gl_accounts):
        visit = _visit(patient, status=1)

        res = auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')

        assert res.status_code == 200
        assert res.data['no_invoice'] is True
        assert Invoice.objects.count() == 0
        assert PatientNote.objects.filter(patient_no=patient).exists()
        assert not ActivePatient.objects.filter(pk=visit.pk).exists()

    def test_an_already_invoiced_visit_is_not_billed_twice(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])
        # The POS billed it on the day it happened.
        prior = Invoice.objects.create(datetime=THREE_DAYS_AGO, patient_no=patient,
                                       grand_total=Decimal('250000'))
        prior.items.create(item=treatment.catalog_item, quantity=1, price=Decimal('250000'))

        res = auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')

        assert res.data['already_invoiced'] is True
        assert Invoice.objects.count() == 1


@pytest.mark.django_db
class TestOrdinaryMarkPaid:
    def test_requires_a_payment_method(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])
        res = auth_api.post(_url(visit.pk), {}, format='json')
        assert res.status_code == 400
        assert ActivePatient.objects.filter(pk=visit.pk).exists()

    def test_creates_an_unposted_invoice_the_sweep_will_pick_up(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])

        res = auth_api.post(_url(visit.pk), {'payment_method_id': gl_accounts['cash_method'].pk},
                            format='json')

        assert res.status_code == 200, res.content
        invoice = Invoice.objects.get(invoice_number=res.data['invoice_number'])
        assert invoice.posting_status == 'unposted'
        assert invoice.payment_account_id == gl_accounts['cash_method'].linked_account_id
        events = _gather_events(timezone.now().date())
        assert any(obj.pk == invoice.pk for day in events.values() for _k, obj in day)


@pytest.mark.django_db
class TestGuards:
    def test_frees_the_beautician_of_a_visit_still_in_treatment(self, auth_api, patient, treatment, gl_accounts):
        beautician = Beauticians.objects.create(beautician_name='Sari', bphone_number='1', available=False)
        visit = _visit(patient, status=4, treatments=[treatment], beautician=beautician)

        auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')

        beautician.refresh_from_db()
        assert beautician.available is True

    @pytest.mark.parametrize('role', ['doctor', 'beautician'])
    def test_clinical_roles_cannot_mark_paid(self, api, patient, treatment, role, gl_accounts):
        user = AppUserFactory(role=role, pin='123456')
        user.generate_token()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {user.auth_token}')
        visit = _visit(patient, treatments=[treatment])

        res = api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')

        assert res.status_code == 403
        assert ActivePatient.objects.filter(pk=visit.pk).exists()

    def test_is_audit_logged(self, auth_api, patient, treatment, gl_accounts):
        visit = _visit(patient, treatments=[treatment])
        auth_api.post(_url(visit.pk), {'exclude_from_journal': True}, format='json')
        assert AuditLog.objects.filter(
            source='app', entity_type='Invoice', description__contains='outside the journal',
        ).exists()


def test_patient_create_accepts_birth_date(auth_api, gl_accounts):
    res = auth_api.post('/api/patients/new/', {
        'name': 'Dina', 'birth_date': '1994-03-02', 'consult_status': False,
    }, format='json')
    assert res.status_code == 201, res.content
    assert Patient.objects.get(name='Dina').birth_date.isoformat() == '1994-03-02'
