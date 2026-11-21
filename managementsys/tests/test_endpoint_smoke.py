import re

import pytest

from managementsys.urls import urlpatterns
from .factories import AppUserFactory


def _collect_routes():
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
    response = superuser_client.get(path)
    assert response.status_code < 500, (
        f"{name} ({path}) returned {response.status_code}\n"
        f"{getattr(response, 'content', b'')[:2000]!r}"
    )
