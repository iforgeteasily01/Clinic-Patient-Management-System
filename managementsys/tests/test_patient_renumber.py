"""Changing a patient's number — the primary key.

The load-bearing test is :meth:`TestRenumber.test_nothing_is_left_behind`:
after a rename, no row anywhere may still point at the old number, and every
row that pointed at it before must point at the new one. It walks the model
metadata rather than a written-down list, so an FK added later is covered by
this test on the day it is added.
"""
import pytest
from django.db import connection

from managementsys.models import (
    ActivePatient, AuditLog, Invoice, MedRec, Patient, PatientNote,
    TreatmentSession,
)
from managementsys.services import patient_renumber
from managementsys.services.patient_renumber import PatientRenumberError

from .factories import AppUserFactory


@pytest.fixture
def patient(db):
    return Patient.objects.create(patient_no='J000001', name='Joko')


@pytest.fixture
def history(patient, db):
    """A patient with something in every shape of table that references them."""
    medrec = MedRec.objects.create(patient_no=patient, subjective='keluhan')
    ActivePatient.objects.create(patient_no=patient, status=1, consult_status=False, medrec=medrec)
    TreatmentSession.objects.create(patient_no=patient)
    Invoice.objects.create(patient_no=patient, datetime='2026-08-01T10:00:00Z', grand_total=100)
    PatientNote.objects.create(patient_no=patient, content='catatan', date='2026-08-01')
    return medrec


@pytest.fixture
def admin(db):
    """The Admin account — ``create_default_admin`` seeds it as a superuser."""
    user = AppUserFactory(display_name='Admin', role='superuser', pin='654321')
    user.generate_token()
    return user


@pytest.fixture
def admin_api(api, admin):
    api.credentials(HTTP_AUTHORIZATION=f'Bearer {admin.auth_token}')
    return api


@pytest.fixture
def manager_api(api, db):
    user = AppUserFactory(role='manager', pin='654321')
    user.generate_token()
    api.credentials(HTTP_AUTHORIZATION=f'Bearer {user.auth_token}')
    return api


def rows_pointing_at(patient_no):
    """{table.column: count} for every FK column holding ``patient_no``."""
    found = {}
    with connection.cursor() as cursor:
        for related in patient_renumber._related_columns():
            table = related['model']._meta.db_table
            column = related['column']
            cursor.execute(f'SELECT COUNT(*) FROM {table} WHERE {column} = %s', [patient_no])
            count = cursor.fetchone()[0]
            if count:
                found[f'{table}.{column}'] = count
    return found


# ── Validation ─────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestValidation:
    def test_blank_is_refused(self, patient):
        with pytest.raises(PatientRenumberError):
            patient_renumber.renumber(patient, '   ')

    def test_the_same_number_is_refused(self, patient):
        with pytest.raises(PatientRenumberError):
            patient_renumber.renumber(patient, 'j000001')

    def test_a_number_in_use_is_refused(self, patient):
        Patient.objects.create(patient_no='B000002', name='Budi')
        with pytest.raises(PatientRenumberError) as exc:
            patient_renumber.renumber(patient, 'B000002')
        assert 'already belongs' in str(exc.value.errors)

    def test_too_long_is_refused(self, patient):
        with pytest.raises(PatientRenumberError):
            patient_renumber.renumber(patient, 'X' * 40)

    def test_punctuation_that_would_break_an_id_is_refused(self, patient):
        with pytest.raises(PatientRenumberError):
            patient_renumber.renumber(patient, 'J 000 1')

    def test_a_legacy_shaped_number_is_accepted(self, patient):
        """The external system is the authority, so the canonical
        {initial}{6 digits} shape is not enforced."""
        patient_renumber.renumber(patient, 'PN00691')
        assert Patient.objects.filter(patient_no='PN00691').exists()

    def test_it_is_normalised_to_upper_case(self, patient):
        patient_renumber.renumber(patient, 'j000105')
        assert Patient.objects.filter(patient_no='J000105').exists()


# ── The rename itself ──────────────────────────────────────────────────────

@pytest.mark.django_db
class TestRenumber:
    def test_nothing_is_left_behind(self, patient, history):
        before = rows_pointing_at('J000001')
        assert before, 'fixture built no history — the test would prove nothing'

        patient_renumber.renumber(patient, 'J000105')

        assert rows_pointing_at('J000001') == {}
        assert rows_pointing_at('J000105') == before

    def test_the_patient_row_moves_rather_than_duplicating(self, patient, history):
        patient_renumber.renumber(patient, 'J000105')

        assert Patient.objects.count() == 1
        assert not Patient.objects.filter(patient_no='J000001').exists()
        assert Patient.objects.get(patient_no='J000105').name == 'Joko'

    def test_the_chart_still_reads_back_through_the_orm(self, patient, history):
        patient_renumber.renumber(patient, 'J000105')

        moved = Patient.objects.get(patient_no='J000105')
        assert moved.medrec_set.count() == 1
        assert moved.invoices.count() == 1
        assert TreatmentSession.objects.filter(patient_no=moved).count() == 1

    def test_medical_record_ids_are_rewritten(self, patient, history):
        assert history.medrec_id.startswith('MR-J000001-')

        summary = patient_renumber.renumber(patient, 'J000105')

        history.refresh_from_db()
        assert history.medrec_id.startswith('MR-J000105-')
        assert summary['medrec_ids_rewritten'] == 1

    def test_only_this_patients_records_are_rewritten(self, patient, history, db):
        """``MR-J0001-`` must not match ``J00012``'s records — hence the
        trailing dash in the prefix."""
        other = Patient.objects.create(patient_no='J0000012', name='Joni')
        other_rec = MedRec.objects.create(patient_no=other)
        original = other_rec.medrec_id

        patient_renumber.renumber(patient, 'J000105')

        other_rec.refresh_from_db()
        assert other_rec.medrec_id == original

    def test_the_visit_date_survives_the_rewrite(self, patient, history):
        """The parsers scan for the 8-digit part, so moving the patient segment
        must not shift the date."""
        from managementsys.views.crm_dashboard import _medrec_date

        before = _medrec_date(history.medrec_id)
        patient_renumber.renumber(patient, 'J000105')
        history.refresh_from_db()

        assert _medrec_date(history.medrec_id) == before

    def test_it_is_audit_logged_with_the_old_number(self, patient, history, admin):
        patient_renumber.renumber(patient, 'J000105', actor=admin)

        log = AuditLog.objects.get(action='RENUMBER')
        assert log.entity_id == 'J000105'
        assert log.performed_by_id == admin.id
        assert log.metadata['old_patient_no'] == 'J000001'
        # A printed medical-record number has to be traceable to its replacement.
        assert log.metadata['medrec_ids']

    def test_a_failure_leaves_everything_untouched(self, patient, history, monkeypatch):
        """The rename is one transaction; a half-renamed chart is not a state
        the clinic is ever allowed to be in."""
        def boom(*args, **kwargs):
            raise RuntimeError('database went away')

        monkeypatch.setattr(patient_renumber, '_rewrite_medrec_ids', boom)

        with pytest.raises(RuntimeError):
            patient_renumber.renumber(patient, 'J000105')

        assert Patient.objects.filter(patient_no='J000001').exists()
        assert rows_pointing_at('J000001')
        assert rows_pointing_at('J000105') == {}

    def test_a_patient_with_no_history_renames_cleanly(self, patient):
        summary = patient_renumber.renumber(patient, 'J000105')
        assert summary['total_rows_moved'] == 0
        assert summary['medrec_ids_rewritten'] == 0


# ── Preview ────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestPreview:
    def test_it_counts_without_moving_anything(self, patient, history):
        result = patient_renumber.preview(patient)

        assert result['total_rows'] > 0
        assert result['medical_records_to_rewrite'] == 1
        assert Patient.objects.filter(patient_no='J000001').exists()

    def test_empty_tables_are_omitted(self, patient):
        result = patient_renumber.preview(patient)
        assert result['related_rows'] == {}
        assert result['total_rows'] == 0


# ── API ────────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestRenumberApi:
    URL = '/api/admin/patients/J000001/renumber/'

    def test_a_cashier_is_refused(self, auth_api, patient):
        """auth_user is a cashier — they edit patients but must not renumber one."""
        assert auth_api.post(self.URL, {'new_patient_no': 'J000105'}, format='json').status_code == 403
        assert Patient.objects.filter(patient_no='J000001').exists()

    def test_anonymous_is_refused(self, api, patient):
        assert api.post(self.URL, {'new_patient_no': 'J000105'}, format='json').status_code in (401, 403)

    def test_a_manager_is_refused(self, manager_api, patient, history):
        """A manager edits patients all day. Renumbering one is not that."""
        assert manager_api.post(self.URL, {'new_patient_no': 'J000105'}, format='json').status_code == 403
        assert Patient.objects.filter(patient_no='J000001').exists()

    def test_a_manager_cannot_even_preview(self, manager_api, patient):
        assert manager_api.get(self.URL).status_code == 403

    def test_the_admin_renumbers(self, admin_api, patient, history):
        response = admin_api.post(self.URL, {'new_patient_no': 'J000105'}, format='json')

        assert response.status_code == 200
        body = response.json()
        assert body['old_patient_no'] == 'J000001'
        assert body['patient']['patient_no'] == 'J000105'
        assert body['total_rows_moved'] > 0

    def test_preview_writes_nothing(self, admin_api, patient, history):
        body = admin_api.get(self.URL).json()

        assert body['patient_no'] == 'J000001'
        assert body['medical_records_to_rewrite'] == 1
        assert Patient.objects.filter(patient_no='J000001').exists()

    def test_a_clash_is_a_readable_400(self, admin_api, patient):
        Patient.objects.create(patient_no='B000002', name='Budi')
        response = admin_api.post(self.URL, {'new_patient_no': 'B000002'}, format='json')

        assert response.status_code == 400
        assert 'already belongs' in str(response.json())

    def test_an_unknown_patient_is_404(self, admin_api, patient):
        response = admin_api.post('/api/admin/patients/Z999999/renumber/',
                                    {'new_patient_no': 'J000105'}, format='json')
        assert response.status_code == 404


# ── The path that used to corrupt data ─────────────────────────────────────

@pytest.mark.django_db
class TestPutCannotRenumber:
    def test_editing_the_patient_with_a_new_number_is_refused(self, manager_api, patient, history):
        """Django would INSERT a second row and orphan the first. It must 400."""
        response = manager_api.put(
            '/api/admin/patients/J000001/',
            {'patient_no': 'J000105', 'name': 'Joko'},
            format='json')

        assert response.status_code == 400
        assert Patient.objects.count() == 1
        assert Patient.objects.filter(patient_no='J000001').exists()

    def test_an_ordinary_edit_still_works(self, manager_api, patient):
        response = manager_api.put(
            '/api/admin/patients/J000001/',
            {'patient_no': 'J000001', 'name': 'Joko Widodo', 'phone_number': '08123456789'},
            format='json')

        assert response.status_code == 200
        patient.refresh_from_db()
        assert patient.name == 'Joko Widodo'

    def test_an_edit_that_omits_the_number_still_works(self, manager_api, patient):
        response = manager_api.put(
            '/api/admin/patients/J000001/', {'name': 'Joko Anwar'}, format='json')

        assert response.status_code == 200
        patient.refresh_from_db()
        assert patient.name == 'Joko Anwar'
