import pytest
from django.core.management import call_command
from django.db import connection


@pytest.mark.django_db
def test_database_connection_is_live():
    with connection.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


@pytest.mark.django_db
def test_no_missing_migrations():
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
