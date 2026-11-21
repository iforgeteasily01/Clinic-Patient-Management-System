import importlib
from decimal import Decimal
from io import StringIO

import pytest
from django.apps import apps as global_apps
from django.core.management import call_command
from django.urls import reverse

from managementsys.models import (
    InventoryItem, Invoice, LedgerEntry, Treatment, TreatmentCategory,
)

from .factories import InventoryBatchFactory, InventoryItemFactory

backfill_migration = importlib.import_module(
    "managementsys.migrations.0069_backfill_treatment_item_category"
)


def _sell(auth_api, stock, gl_accounts, item_id, *, price, grand_total):
    res = auth_api.post(
        reverse("invoice-create"),
        {
            "warehouse_id": stock["warehouse"].id,
            "payment_method_id": gl_accounts["cash_method"].id,
            "discount": 0, "tax": 0, "additional_charges": 0,
            "grand_total": grand_total,
            "items": [{"item_id": item_id, "price": price, "quantity": 1}],
        },
        format="json",
    )
    if res.status_code in (200, 201):
        import datetime
        run = auth_api.post(
            reverse("accounting-journal-run"),
            {"date_to": datetime.date.today().isoformat()}, format="json",
        )
        assert run.status_code == 200, run.content
    return res


@pytest.mark.django_db
class TestTreatmentCategoryLinkage:
    def test_saving_a_treatment_links_its_category(self, db):
        treatment = Treatment.objects.create(
            code="LASER", name="Laser", category="Laser", price=Decimal("50000"),
        )
        treatment.refresh_from_db()

        cat = TreatmentCategory.objects.get(name="Laser")
        assert treatment.catalog_item.item_category_id == cat.id
        assert cat.revenue_account_id
        assert not hasattr(cat, "cogs_account")
        assert not hasattr(cat, "expense_account")

    def test_category_match_is_case_insensitive(self, db):
        Treatment.objects.create(code="A", name="A", category="Facial", price=Decimal("1"))
        Treatment.objects.create(code="B", name="B", category="facial", price=Decimal("1"))
        assert TreatmentCategory.objects.filter(name__iexact="facial").count() == 1

    def test_blank_category_leaves_item_uncategorised(self, db):
        treatment = Treatment.objects.create(
            code="MISC", name="Misc", category="", price=Decimal("1"),
        )
        treatment.refresh_from_db()
        assert treatment.catalog_item.item_category_id is None


@pytest.mark.django_db
class TestPosRoutesToCategoryAccounts:
    @pytest.fixture
    def laser(self, stock, gl_accounts):
        treatment = Treatment.objects.create(
            code="LASER", name="Laser", category="Laser", price=Decimal("50000"),
        )
        treatment.refresh_from_db()
        return treatment

    def test_revenue_hits_category_account_and_no_cogs_is_posted(
        self, auth_api, stock, gl_accounts, laser
    ):
        cat = TreatmentCategory.objects.get(name="Laser")

        res = _sell(auth_api, stock, gl_accounts, laser.catalog_item.id,
                    price=50000, grand_total=50000)
        assert res.status_code in (200, 201), res.content
        assert Invoice.objects.count() == 1

        cat.revenue_account.refresh_from_db()
        gl_accounts["revenue"].refresh_from_db()
        gl_accounts["cogs"].refresh_from_db()

        assert cat.revenue_account.balance == Decimal("50000")
        assert gl_accounts["revenue"].balance == Decimal("0")

        assert gl_accounts["cogs"].balance == Decimal("0")
        assert not LedgerEntry.objects.filter(account=gl_accounts["cogs"]).exists()

    def test_service_line_consumes_no_stock(
        self, auth_api, stock, gl_accounts, laser
    ):
        before = stock["batch"].quantity_remaining

        _sell(auth_api, stock, gl_accounts, laser.catalog_item.id,
              price=50000, grand_total=50000)

        stock["batch"].refresh_from_db()
        assert stock["batch"].quantity_remaining == before


@pytest.mark.django_db
class TestInventoryItemsAreOneEntity:
    def test_physical_item_ignores_category_and_uses_product_accounts(
        self, auth_api, stock, gl_accounts
    ):
        cat = TreatmentCategory.objects.create(name="Skincare Products")
        InventoryItem.objects.filter(pk=stock["item"].id).update(item_category=cat)

        res = _sell(auth_api, stock, gl_accounts, stock["item"].id,
                    price=10000, grand_total=10000)
        assert res.status_code in (200, 201), res.content

        gl_accounts["revenue"].refresh_from_db()
        gl_accounts["cogs"].refresh_from_db()
        cat.revenue_account.refresh_from_db()

        assert gl_accounts["revenue"].balance == Decimal("10000")
        assert gl_accounts["cogs"].balance == Decimal("5000")
        assert cat.revenue_account.balance == Decimal("0")


@pytest.mark.django_db
class TestBackfillMigration:
    def test_links_existing_category(self, db):
        treatment = Treatment.objects.create(
            code="BTX", name="Botox", category="Botox", price=Decimal("100"),
        )
        treatment.refresh_from_db()
        cat = TreatmentCategory.objects.get(name="Botox")
        InventoryItem.objects.filter(pk=treatment.catalog_item_id).update(item_category=None)

        backfill_migration.backfill(global_apps, None)

        item = InventoryItem.objects.get(pk=treatment.catalog_item_id)
        assert item.item_category_id == cat.id

    def test_creates_missing_category_with_accounts(self, db):
        treatment = Treatment.objects.create(
            code="PEEL", name="Peeling", category="Peeling", price=Decimal("100"),
        )
        treatment.refresh_from_db()
        item_id = treatment.catalog_item_id
        TreatmentCategory.objects.filter(name="Peeling").delete()
        InventoryItem.objects.filter(pk=item_id).update(item_category=None)
        assert not TreatmentCategory.objects.filter(name__iexact="Peeling").exists()

        backfill_migration.backfill(global_apps, None)

        cat = TreatmentCategory.objects.get(name="Peeling")
        assert cat.revenue_account_id
        assert InventoryItem.objects.get(pk=item_id).item_category_id == cat.id


@pytest.mark.django_db
class TestProvisionCommand:
    def _legacy_treatment(self, name, category):
        treatment = Treatment.objects.create(
            code=name, name=name, category=category, price=Decimal("1"),
        )
        treatment.refresh_from_db()
        TreatmentCategory.objects.filter(name__iexact=category).delete()
        InventoryItem.objects.filter(pk=treatment.catalog_item_id).update(item_category=None)
        return treatment

    def test_generates_provisions_and_links(self, db):
        treatment = self._legacy_treatment("BTX", "Botox")
        assert not TreatmentCategory.objects.filter(name__iexact="Botox").exists()

        call_command('provision_category_accounts', stdout=StringIO())

        cat = TreatmentCategory.objects.get(name="Botox")
        assert cat.revenue_account_id
        assert InventoryItem.objects.get(pk=treatment.catalog_item_id).item_category_id == cat.id

    def test_dry_run_writes_nothing(self, db):
        self._legacy_treatment("PEEL", "Peeling")

        call_command('provision_category_accounts', '--dry-run', stdout=StringIO())

        assert not TreatmentCategory.objects.filter(name__iexact="Peeling").exists()
