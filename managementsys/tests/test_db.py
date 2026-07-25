"""Database connectivity and schema-drift smoke tests."""
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
    """Fails if models have drifted from the committed migrations.

    makemigrations --check reads the migration history table, so DB access is
    required even though the schema itself is built with --nomigrations.
    """
    call_command("makemigrations", "--check", "--dry-run", verbosity=0)
