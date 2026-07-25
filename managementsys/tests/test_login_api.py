"""Integration tests for the auth endpoints (real DB, real DRF stack)."""
import pytest
from django.urls import reverse

from managementsys.models import AuditLog
from .factories import AppUserFactory


@pytest.mark.django_db
class TestLoginEndpoint:
    def test_valid_pin_returns_token_and_audits(self, api):
        user = AppUserFactory(pin="654321")

        res = api.post(
            reverse("auth-login"),
            {"user_id": user.id, "pin": "654321"},
            format="json",
        )

        assert res.status_code == 200
        body = res.json()
        assert body["token"]
        assert body["user"]["id"] == user.id

        user.refresh_from_db()
        assert user.auth_token == body["token"]            # token persisted
        assert AuditLog.objects.filter(action="LOGIN", performed_by=user).exists()

    def test_wrong_pin_is_401_and_issues_no_token(self, api):
        user = AppUserFactory(pin="654321")

        res = api.post(
            reverse("auth-login"),
            {"user_id": user.id, "pin": "000000"},
            format="json",
        )

        assert res.status_code == 401
        user.refresh_from_db()
        assert user.auth_token == ""

    def test_unknown_user_is_401(self, api):
        res = api.post(
            reverse("auth-login"),
            {"user_id": 999999, "pin": "123456"},
            format="json",
        )
        assert res.status_code == 401

    def test_missing_fields_is_400(self, api):
        res = api.post(reverse("auth-login"), {"user_id": 1}, format="json")
        assert res.status_code == 400

    def test_inactive_user_cannot_log_in(self, api):
        user = AppUserFactory(pin="654321", is_active=False)
        res = api.post(
            reverse("auth-login"),
            {"user_id": user.id, "pin": "654321"},
            format="json",
        )
        assert res.status_code == 401


@pytest.mark.django_db
class TestBearerAuth:
    def test_valid_token_authorizes_protected_endpoint(self, auth_api):
        # /api/auth/users/ is readable; the point is the request is accepted.
        res = auth_api.get(reverse("auth-users"))
        assert res.status_code == 200

    def test_logout_audits_but_intentionally_keeps_token_alive(self, auth_api, auth_user):
        # By design (see LogoutView), the token is NOT cleared so other clients
        # sharing it (e.g. the WPF cashier app) stay authenticated. Logout only
        # writes an audit record.
        from managementsys.models import AuditLog

        token_before = auth_user.auth_token
        res = auth_api.post(reverse("auth-logout"))

        assert res.status_code in (200, 204)
        auth_user.refresh_from_db()
        assert auth_user.auth_token == token_before
        assert AuditLog.objects.filter(action="LOGOUT", performed_by=auth_user).exists()
