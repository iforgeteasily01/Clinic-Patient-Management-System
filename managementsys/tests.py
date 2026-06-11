from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from .models import ActivePatient, AppUser, Patient


def _make_user(token='testtoken123'):
    user = AppUser.objects.create(
        display_name='Test Admin',
        role='superuser',
        auth_token=token,
    )
    user.set_pin('000000')
    user.save()
    return user


def _auth_client(token='testtoken123'):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f'Bearer {token}')
    return client


class PatientCreateWithActiveViewTests(TestCase):
    """
    Tests for POST /api/patients/new/
    Covers all cases that could cause errors on the new-patient form.
    """

    def setUp(self):
        self.user = _make_user()
        self.client = _auth_client()
        self.url = '/api/patients/new/'

    # ── Happy-path tests ──────────────────────────────────────────────────────

    def test_create_patient_all_fields(self):
        """Full payload with all fields creates patient + active patient."""
        res = self.client.post(self.url, {
            'name': 'Jane Doe',
            'address': '123 Main St',
            'phone_number': '08123456789',
            'NIK': '1234567890123456',
            'consult_status': False,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        data = res.json()
        self.assertIn('patient', data)
        self.assertIn('active_patient', data)

        patient_no = data['patient']['patient_no']
        self.assertTrue(patient_no.startswith('J'), patient_no)

        # ActivePatient should exist in DB
        active = ActivePatient.objects.get(patient_no__patient_no=patient_no)
        self.assertEqual(active.status, 3)   # treatment queue
        self.assertFalse(active.consult_status)

    def test_create_patient_minimal_fields(self):
        """Only name is required; other fields may be omitted."""
        res = self.client.post(self.url, {'name': 'Alice'}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        data = res.json()
        patient_no = data['patient']['patient_no']
        self.assertTrue(patient_no, 'patient_no should be auto-generated')

    def test_consult_status_true_sets_status_1(self):
        """consult_status=True should create ActivePatient with status=1."""
        res = self.client.post(self.url, {
            'name': 'Bob',
            'consult_status': True,
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        active = ActivePatient.objects.get(
            patient_no__patient_no=res.json()['patient']['patient_no']
        )
        self.assertEqual(active.status, 1)
        self.assertTrue(active.consult_status)

    def test_consult_status_false_sets_status_3(self):
        """consult_status=False (default) creates ActivePatient with status=3."""
        res = self.client.post(self.url, {'name': 'Carol', 'consult_status': False}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        active = ActivePatient.objects.get(
            patient_no__patient_no=res.json()['patient']['patient_no']
        )
        self.assertEqual(active.status, 3)

    def test_omitting_consult_status_defaults_to_treatment(self):
        """Omitting consult_status should default to status=3 (treatment queue)."""
        res = self.client.post(self.url, {'name': 'Dave'}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        active = ActivePatient.objects.get(
            patient_no__patient_no=res.json()['patient']['patient_no']
        )
        self.assertEqual(active.status, 3)

    def test_patient_no_is_auto_generated(self):
        """patient_no should be ignored in input and auto-generated from name."""
        res = self.client.post(self.url, {
            'name': 'Eve',
            'patient_no': 'CUSTOM99',   # should be ignored
        }, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        returned_no = res.json()['patient']['patient_no']
        self.assertNotEqual(returned_no, 'CUSTOM99')
        self.assertTrue(returned_no.startswith('E'), returned_no)

    def test_creates_exactly_one_patient_and_one_active_patient(self):
        """Each POST creates exactly one Patient and one ActivePatient."""
        Patient.objects.all().delete()
        ActivePatient.objects.all().delete()
        self.client.post(self.url, {'name': 'Frank'}, format='json')
        self.assertEqual(Patient.objects.count(), 1)
        self.assertEqual(ActivePatient.objects.count(), 1)

    # ── Validation error tests ────────────────────────────────────────────────

    def test_missing_name_returns_400(self):
        """Omitting name entirely should return 400."""
        res = self.client.post(self.url, {
            'address': '456 Oak Ave',
            'phone_number': '08111111111',
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_empty_name_returns_400(self):
        """Sending name='' should return 400 (blank not allowed)."""
        res = self.client.post(self.url, {'name': ''}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_name_too_long_returns_400(self):
        """Name exceeding 100 chars should fail serializer validation."""
        res = self.client.post(self.url, {'name': 'A' * 101}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_phone_too_long_returns_400(self):
        """phone_number exceeding 15 chars should fail serializer validation."""
        res = self.client.post(self.url, {
            'name': 'Grace',
            'phone_number': '0' * 16,
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_nik_too_long_returns_400(self):
        """NIK exceeding 16 chars should fail serializer validation."""
        res = self.client.post(self.url, {
            'name': 'Hank',
            'NIK': '1' * 17,
        }, format='json')
        self.assertEqual(res.status_code, 400)

    # ── Auth tests ────────────────────────────────────────────────────────────

    def test_unauthenticated_request_returns_403(self):
        """Request without auth token should be rejected."""
        unauth_client = APIClient()
        res = unauth_client.post(self.url, {'name': 'Ivan'}, format='json')
        self.assertIn(res.status_code, [401, 403])

    # ── No orphaned records on failure ────────────────────────────────────────

    def test_no_patient_left_when_validation_fails(self):
        """If patient validation fails, no Patient record should be created."""
        before = Patient.objects.count()
        self.client.post(self.url, {}, format='json')   # no name
        self.assertEqual(Patient.objects.count(), before)


class PatientSearchViewTests(TestCase):
    """
    Tests for GET /api/patients/search/
    Verifies that the search+field query format sent by the frontend works.
    """

    def setUp(self):
        self.user = _make_user(token='searchtoken')
        self.client = _auth_client('searchtoken')
        self.url = '/api/patients/search/'
        # Create a few patients
        Patient.objects.create(patient_no='J000001', name='John Smith', phone_number='08111111111')
        Patient.objects.create(patient_no='A000001', name='Alice Brown', phone_number='08222222222')
        Patient.objects.create(patient_no='B000001', name='Bob Jones', phone_number='08333333333')

    def test_search_by_name_with_search_param(self):
        """?search=john&field=name should return patients whose name contains 'john'."""
        res = self.client.get(self.url, {'search': 'john', 'field': 'name'})
        self.assertEqual(res.status_code, 200)
        names = [p['name'] for p in res.json()]
        self.assertIn('John Smith', names)
        self.assertNotIn('Alice Brown', names)

    def test_search_by_patient_no_with_search_param(self):
        """?search=J000&field=patient_no should filter by patient_no."""
        res = self.client.get(self.url, {'search': 'J000', 'field': 'patient_no'})
        self.assertEqual(res.status_code, 200)
        patient_nos = [p['patient_no'] for p in res.json()]
        self.assertIn('J000001', patient_nos)
        self.assertNotIn('A000001', patient_nos)

    def test_search_by_phone_with_search_param(self):
        """?search=08222&field=phone should filter by phone_number."""
        res = self.client.get(self.url, {'search': '08222', 'field': 'phone'})
        self.assertEqual(res.status_code, 200)
        names = [p['name'] for p in res.json()]
        self.assertIn('Alice Brown', names)
        self.assertNotIn('John Smith', names)

    def test_legacy_name_param_still_works(self):
        """Old ?name=X format (used by other callers) should still work."""
        res = self.client.get(self.url, {'name': 'Bob'})
        self.assertEqual(res.status_code, 200)
        names = [p['name'] for p in res.json()]
        self.assertIn('Bob Jones', names)

    def test_empty_search_returns_all(self):
        """No search params should return all patients (up to 100)."""
        res = self.client.get(self.url)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()), 3)
