"""Per-category GL routing for treatments sold via POS.

A treatment's mirror catalog item must carry the ``TreatmentCategory`` matching
``Treatment.category`` so that its POS revenue and material COGS post to that
category's own GL accounts instead of the system fallbacks (4200000 / 5100000).
"""
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
        # Phase 2: posting is deferred — sweep it so the GL assertions below
        # (which check posted account balances) see the real effect.
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
        # save() auto-provisions the category's revenue account. COGS/expense
        # are no longer per-category as of Phase 3 (see test_expense_accounting.py).
        assert cat.revenue_account_id
        assert not hasattr(cat, "cogs_account")
        assert not hasattr(cat, "expense_account")

    def test_category_match_is_case_insensitive(self, db):
        Treatment.objects.create(code="A", name="A", category="Facial", price=Decimal("1"))
        Treatment.objects.create(code="B", name="B", category="facial", price=Decimal("1"))
        # Both map to the single existing category, not two.
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
        """A 'Laser' treatment, sold as a service line."""
        treatment = Treatment.objects.create(
            code="LASER", name="Laser", category="Laser", price=Decimal("50000"),
        )
        treatment.refresh_from_db()
        return treatment

    def test_revenue_hits_category_account_and_no_cogs_is_posted(
        self, auth_api, stock, gl_accounts, laser
    ):
        """Revenue routes per category; a treatment posts no cost of sales.

        Both halves matter. The first is the point of per-category accounts. The
        second is what migration 0107 established when it dropped
        ``TreatmentMaterial``: a fixed per-treatment recipe produced a cost that
        was precise, automatic and wrong, so treatment COGS is now entered by
        hand against 5000200. This asserts the absence deliberately — a
        reintroduced bill-of-materials would show up here as a non-zero balance
        rather than as a quietly changed gross margin.
        """
        cat = TreatmentCategory.objects.get(name="Laser")

        res = _sell(auth_api, stock, gl_accounts, laser.catalog_item.id,
                    price=50000, grand_total=50000)
        assert res.status_code in (200, 201), res.content
        assert Invoice.objects.count() == 1

        cat.revenue_account.refresh_from_db()
        gl_accounts["revenue"].refresh_from_db()
        gl_accounts["cogs"].refresh_from_db()

        # Sale price to the category's revenue account; nothing to the fallback.
        assert cat.revenue_account.balance == Decimal("50000")
        assert gl_accounts["revenue"].balance == Decimal("0")

        # No recipe, so no COGS leg at all — not merely a zero balance.
        assert gl_accounts["cogs"].balance == Decimal("0")
        assert not LedgerEntry.objects.filter(account=gl_accounts["cogs"]).exists()

    def test_service_line_consumes_no_stock(
        self, auth_api, stock, gl_accounts, laser
    ):
        """The stock half of the same rule.

        Service lines stopped drawing on inventory with 0107. Batches now move
        only on product sales, purchases and stock corrections.
        """
        before = stock["batch"].quantity_remaining

        _sell(auth_api, stock, gl_accounts, laser.catalog_item.id,
              price=50000, grand_total=50000)

        stock["batch"].refresh_from_db()
        assert stock["batch"].quantity_remaining == before


@pytest.mark.django_db
class TestInventoryItemsAreOneEntity:
    """Physical inventory items are a single entity: they post to the shared
    product accounts (4200000 / 5100000) even when tagged with a category."""

    def test_physical_item_ignores_category_and_uses_product_accounts(
        self, auth_api, stock, gl_accounts
    ):
        cat = TreatmentCategory.objects.create(name="Skincare Products")
        # Tag the physical stock item with a category — it must NOT divert routing.
        InventoryItem.objects.filter(pk=stock["item"].id).update(item_category=cat)

        # Sell one unit @ 10000 -> COGS 5000 (batch is 5000/unit).
        res = _sell(auth_api, stock, gl_accounts, stock["item"].id,
                    price=10000, grand_total=10000)
        assert res.status_code in (200, 201), res.content

        gl_accounts["revenue"].refresh_from_db()
        gl_accounts["cogs"].refresh_from_db()
        cat.revenue_account.refresh_from_db()

        assert gl_accounts["revenue"].balance == Decimal("10000")   # product revenue
        assert gl_accounts["cogs"].balance == Decimal("5000")       # product COGS
        assert cat.revenue_account.balance == Decimal("0")          # category untouched


@pytest.mark.django_db
class TestBackfillMigration:
    """The 0069 data backfill links legacy treatments whose mirror item was
    created before the category linkage existed."""

    def test_links_existing_category(self, db):
        treatment = Treatment.objects.create(
            code="BTX", name="Botox", category="Botox", price=Decimal("100"),
        )
        treatment.refresh_from_db()
        cat = TreatmentCategory.objects.get(name="Botox")
        # Simulate legacy state: mirror item created without a category link.
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
        # Simulate a category string that has no TreatmentCategory at all.
        TreatmentCategory.objects.filter(name="Peeling").delete()  # SET_NULLs the mirror link
        InventoryItem.objects.filter(pk=item_id).update(item_category=None)
        assert not TreatmentCategory.objects.filter(name__iexact="Peeling").exists()

        backfill_migration.backfill(global_apps, None)

        cat = TreatmentCategory.objects.get(name="Peeling")
        assert cat.revenue_account_id
        assert InventoryItem.objects.get(pk=item_id).item_category_id == cat.id


@pytest.mark.django_db
class TestProvisionCommand:
    """`provision_category_accounts` generates in-use categories, provisions
    their accounts, and links treatment items. Idempotent."""

    def _legacy_treatment(self, name, category):
        treatment = Treatment.objects.create(
            code=name, name=name, category=category, price=Decimal("1"),
        )
        treatment.refresh_from_db()
        # Reset to the pre-linkage state this command exists to repair.
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
