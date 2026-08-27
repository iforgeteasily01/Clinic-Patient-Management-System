"""Automatic activity logging and the read API it feeds.

Two things are load-bearing here and neither is obvious from the code:

* the middleware must **never** be able to fail a request, and
* it must **never** store a PIN.

Both get a test of their own.
"""
import pytest

from managementsys.activity_log import _redact, describe
from managementsys.models import AuditLog

from .factories import AppUserFactory


# ── describe(): URL → (action, entity, id) ─────────────────────────────────

class TestDescribe:
    @pytest.mark.parametrize('method,path,expected', [
        ('POST',   '/api/patients/new/',        ('CREATE', 'Patient', '')),
        ('POST',   '/api/invoices/create/',     ('CREATE', 'Invoice', '')),
        ('PATCH',  '/api/invoices/42/',         ('UPDATE', 'Invoice', '42')),
        ('DELETE', '/api/invoices/42/',         ('DELETE', 'Invoice', '42')),
        ('POST',   '/api/billing/7/complete/',  ('COMPLETE', 'Billing', '7')),
        ('POST',   '/api/inventory/stock-out/', ('CREATE', 'Inventory', '')),
    ])
    def test_reads_the_request_line(self, method, path, expected):
        assert describe(method, path) == expected

    def test_a_trailing_verb_beats_the_method(self):
        """POST .../void is a VOID. Calling it a CREATE would be a lie in the log."""
        action, entity, entity_id = describe('POST', '/api/invoices/12/void/')
        assert action == 'VOID'
        assert entity == 'Invoice'
        assert entity_id == '12'

    def test_unknown_resource_still_gets_a_usable_name(self):
        action, entity, _ = describe('POST', '/api/widget-frobnicators/')
        assert action == 'CREATE'
        assert entity == 'WidgetFrobnicators'


# ── Redaction ──────────────────────────────────────────────────────────────

class TestRedaction:
    def test_masks_anything_credential_shaped(self):
        out = _redact({'pin': '123456', 'new_pin': '9', 'api_key': 'k', 'name': 'Budi'})
        assert out == {'pin': '***', 'new_pin': '***', 'api_key': '***', 'name': 'Budi'}

    def test_masks_nested_values(self):
        out = _redact({'user': {'display_name': 'Ani', 'password': 'hunter2'}})
        assert out['user'] == {'display_name': 'Ani', 'password': '***'}

    def test_long_lists_are_trimmed_not_stored_whole(self):
        out = _redact({'items': [{'i': n} for n in range(50)]})
        assert len(out['items']) == 6           # 5 items + the "... more" marker
        assert '45 more' in out['items'][-1]


# ── The middleware, end to end ─────────────────────────────────────────────

@pytest.mark.django_db
class TestMiddlewareRecords:
    def test_a_mutating_request_is_logged_with_its_actor(self, auth_api, auth_user):
        auth_api.post('/api/patients/new/', {}, format='json')

        row = AuditLog.objects.filter(source=AuditLog.SOURCE_HTTP).first()
        assert row is not None
        assert row.method == 'POST'
        assert row.path == '/api/patients/new/'
        assert row.performed_by_id == auth_user.id
        assert row.duration_ms is not None
        assert row.status_code is not None

    def test_a_failed_request_is_logged_as_a_failure(self, auth_api):
        """A 400 is exactly the case somebody will come looking for later."""
        auth_api.post('/api/invoices/create/', {'items': []}, format='json')

        row = AuditLog.objects.filter(source=AuditLog.SOURCE_HTTP).first()
        assert row is not None
        assert row.is_failure
        assert 'failed' in row.description

    def test_reads_are_not_logged(self, auth_api):
        auth_api.get('/api/patients/search/?search=x')
        assert not AuditLog.objects.filter(source=AuditLog.SOURCE_HTTP).exists()

    def test_login_is_left_to_the_view(self, api, db):
        """LoginView writes its own richer LOGIN row; two rows for one event is noise."""
        user = AppUserFactory(pin='654321')
        api.post('/api/auth/login/', {'user_id': user.id, 'pin': '654321'}, format='json')

        assert not AuditLog.objects.filter(source=AuditLog.SOURCE_HTTP).exists()
        assert AuditLog.objects.filter(action='LOGIN').count() == 1

    def test_a_pin_never_reaches_the_table(self, auth_api, auth_user):
        auth_api.patch(f'/api/admin/users/{auth_user.id}/',
                       {'display_name': 'Renamed', 'pin': '999999'}, format='json')

        for row in AuditLog.objects.filter(source=AuditLog.SOURCE_HTTP):
            assert '999999' not in str(row.metadata)

    def test_a_logging_failure_does_not_fail_the_request(self, auth_api, monkeypatch):
        """The log is a bystander. If it breaks, the clinic keeps working."""
        import managementsys.activity_log as module

        def boom(*args, **kwargs):
            raise RuntimeError('table is on fire')

        monkeypatch.setattr(module.ActivityLogMiddleware, '_record', boom)
        response = auth_api.post('/api/patients/new/', {}, format='json')

        assert response.status_code != 500

    def test_the_flag_turns_it_off(self, auth_api, settings):
        settings.ACTIVITY_LOG_ENABLED = False
        auth_api.post('/api/patients/new/', {}, format='json')
        assert not AuditLog.objects.filter(source=AuditLog.SOURCE_HTTP).exists()


# ── The read API ───────────────────────────────────────────────────────────

@pytest.fixture
def manager_api(api, db):
    user = AppUserFactory(role='manager', pin='654321')
    user.generate_token()
    api.credentials(HTTP_AUTHORIZATION=f'Bearer {user.auth_token}')
    return api


@pytest.mark.django_db
class TestActivityLogApi:
    URL = '/api/admin/activity-log/'

    def _seed(self, user=None):
        AuditLog.objects.create(action='CREATE', entity_type='Patient', entity_id='1',
                                description='created a patient', performed_by=user)
        AuditLog.objects.create(action='VOID', entity_type='Invoice', entity_id='9',
                                description='voided an invoice', performed_by=user,
                                source=AuditLog.SOURCE_HTTP, status_code=200,
                                method='POST', path='/api/invoices/9/void/')
        AuditLog.objects.create(action='UPDATE', entity_type='Invoice', entity_id='9',
                                description='failed edit', source=AuditLog.SOURCE_HTTP,
                                status_code=400, method='PATCH', path='/api/invoices/9/')

    def test_a_cashier_is_refused(self, auth_api):
        """auth_user is a cashier. The log is every actor's movements."""
        assert auth_api.get(self.URL).status_code == 403

    def test_anonymous_is_refused(self, api):
        assert api.get(self.URL).status_code in (401, 403)

    def test_a_manager_reads_it(self, manager_api):
        self._seed()
        body = manager_api.get(self.URL).json()
        assert body['count'] == 3
        assert len(body['results']) == 3

    def test_filters_combine(self, manager_api):
        self._seed()
        body = manager_api.get(self.URL, {'entity_type': 'Invoice', 'status': 'error'}).json()
        assert body['count'] == 1
        assert body['results'][0]['description'] == 'failed edit'

    def test_free_text_search(self, manager_api):
        self._seed()
        body = manager_api.get(self.URL, {'q': 'voided'}).json()
        assert body['count'] == 1

    def test_paging_reports_whether_more_exists(self, manager_api):
        self._seed()
        body = manager_api.get(self.URL, {'page_size': 2}).json()
        assert len(body['results']) == 2
        assert body['has_next'] is True

    def test_reading_the_log_does_not_write_to_it(self, manager_api):
        self._seed()
        before = AuditLog.objects.count()
        manager_api.get(self.URL)
        assert AuditLog.objects.count() == before

    def test_meta_lists_what_can_be_filtered_on(self, manager_api):
        self._seed()
        body = manager_api.get(self.URL + 'meta/').json()
        assert set(body['actions']) == {'CREATE', 'VOID', 'UPDATE'}
        assert 'Invoice' in body['entity_types']
        assert body['total_rows'] == 3


# ── Health / status ────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestSystemEndpoints:
    def test_health_needs_no_token(self, api):
        """The launcher polls this before anyone has logged in."""
        response = api.get('/api/system/health/')
        assert response.status_code == 200
        assert response.json()['status'] == 'ok'

    def test_health_carries_nothing_about_the_clinic(self, api):
        body = api.get('/api/system/health/').json()
        assert set(body) == {'status', 'database', 'server_time', 'uptime_seconds'}

    def test_status_refuses_a_cashier(self, auth_api):
        assert auth_api.get('/api/system/status/').status_code == 403

    def test_status_allows_a_manager(self, manager_api):
        assert manager_api.get('/api/system/status/').status_code == 200

    def test_status_reports_the_database_and_the_counts(self, manager_api):
        body = manager_api.get('/api/system/status/').json()
        assert body['databases']['default']['ok'] is True
        assert 'patients' in body['counts']
        assert body['versions']['django']
