"""Smoke test: hit EVERY backend endpoint and assert none return 5xx.

Goal: confirm the whole Django/DRF backend responds without crashing on a
plain authenticated GET. Each route from ``managementsys.urls.urlpatterns`` is
turned into a concrete path (path converters replaced by a sample value) and
parametrized into its own test case.

PASS  = status_code < 500  (200/201/3xx/400/401/403/404/405 are all fine —
        e.g. GET on a POST-only endpoint legitimately returns 405).
FAIL  = status_code >= 500 (the view crashed).
"""
import re

import pytest

from managementsys.urls import urlpatterns
from .factories import AppUserFactory


def _collect_routes():
    """Enumerate (name, path) for every URL pattern.

    Replace every ``<...>`` path converter with the literal ``1`` and prefix
    with ``/``. The empty homepage route ('') becomes '/'.
    """
    routes = []
    for p in urlpatterns:
        route = str(p.pattern)
        concrete = re.sub(r"<[^>]+>", "1", route)
        path = "/" + concrete
        name = p.name or concrete or "homepage"
        routes.append((name, path))
    return routes


ROUTES = _collect_routes()


@pytest.fixture
def superuser_client(db):
    """An APIClient authenticated as a superuser AppUser (passes role checks)."""
    from rest_framework.test import APIClient

    user = AppUserFactory(pin="654321", role="superuser")
    user.generate_token()
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {user.auth_token}")
    return client


@pytest.mark.django_db
@pytest.mark.parametrize(
    "name,path",
    ROUTES,
    ids=[name for name, _ in ROUTES],
)
def test_endpoint_does_not_5xx(superuser_client, name, path):
    """GET each endpoint; only a 5xx server error is a failure."""
    response = superuser_client.get(path)
    assert response.status_code < 500, (
        f"{name} ({path}) returned {response.status_code}\n"
        f"{getattr(response, 'content', b'')[:2000]!r}"
    )
