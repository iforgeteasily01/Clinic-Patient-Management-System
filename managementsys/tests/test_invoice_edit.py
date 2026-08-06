"""Integration tests for editing an invoice: PUT /api/invoices/<pk>/.

An edit reverses the invoice's original stock + ledger postings and re-posts the
new lines. The invariant these tests defend is that reverse-then-repost is
value-neutral: create -> edit -> void must leave every account balance and every
batch exactly where it started.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    ChartOfAccounts, InventoryBatch, Invoice, InvoiceItem, Patient,
    PatientPackage, PatientPackageRedemption, Treatment, TreatmentMaterial,
    TreatmentPackage, TreatmentPackageItem,
)

from .factories import InventoryBatchFactory, InventoryItemFactory


def _run_journal(auth_api, through=None):
    """Phase 2: creation is deferred (posting_status='unposted', zero
    LedgerEntry rows, stock untouched) until this runs. A subsequent void/edit
    of a *posted* invoice then takes the same-day memo path, which does
    perform the real FIFO restock/deduct immediately (only the ledger rows are
    memo-dated) — these edit/void tests need that, so they sweep first,
    mirroring how the app is actually used."""
    date_to = (through or datetime.date.today()).isoformat()
    res = auth_api.post(reverse("accounting-journal-run"), {"date_to": date_to}, format="json")
    assert res.status_code == 200, res.content
    return res.json()


def _create_payload(stock, gl_accounts, *, item_id=None, quantity, price, grand_total):
    return {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash_method"].id,
        "discount": 0,
        "tax": 0,
        "additional_charges": 0,
        "grand_total": grand_total,
        "items": [
            {"item_id": item_id or stock["item"].id, "price": price, "quantity": quantity},
        ],
    }


def _balances():
    return {a.pk: a.balance for a in ChartOfAccounts.objects.all()}


def _batches():
    return {b.pk: b.quantity_remaining for b in InventoryBatch.objects.all()}


@pytest.fixture
def sold_invoice(auth_api, stock, gl_accounts):
    """An invoice selling 2 units @ 10000 out of the 100-unit batch."""
    res = auth_api.post(
        reverse("invoice-create"),
        _create_payload(stock, gl_accounts, quantity=2, price=10000, grand_total=20000),
        format="json",
    )
    assert res.status_code in (200, 201), res.content
    _run_journal(auth_api)
    return Invoice.objects.latest("id")


@pytest.mark.django_db
class TestInvoiceEditItems:
    def test_quantity_change_restocks_old_and_deducts_new(self, auth_api, stock, gl_accounts, sold_invoice):
        # 100 - 2 = 98 after the sale; editing to 5 should land at 95, not 93.
        assert InventoryBatch.objects.get(pk=stock["batch"].pk).quantity_remaining == Decimal("98.0000")

        res = auth_api.put(
            reverse("invoice-detail", args=[sold_invoice.pk]),
            {"grand_total": 50000, "items": [
                {"item_id": stock["item"].id, "quantity": 5, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content

        assert InventoryBatch.objects.get(pk=stock["batch"].pk).quantity_remaining == Decimal("95.0000")
        # Cash reflects the new total only — the old 20000 was reversed.
        gl_accounts["cash"].refresh_from_db()
        assert gl_accounts["cash"].balance == Decimal("50000")
        gl_accounts["revenue"].refresh_from_db()
        assert gl_accounts["revenue"].balance == Decimal("50000")

    def test_swapping_the_item_moves_stock_between_batches(self, auth_api, stock, gl_accounts, sold_invoice):
        other = InventoryItemFactory(selling_price=10000)
        other_batch = InventoryBatchFactory(
            item=other, warehouse=stock["warehouse"],
            quantity_initial=Decimal("100"), quantity_remaining=Decimal("100"),
            value=Decimal("500000"),
        )

        res = auth_api.put(
            reverse("invoice-detail", args=[sold_invoice.pk]),
            {"items": [{"item_id": other.id, "quantity": 2, "price": 10000}]},
            format="json",
        )
        assert res.status_code == 200, res.content

        # Original item fully restored, new item deducted.
        assert InventoryBatch.objects.get(pk=stock["batch"].pk).quantity_remaining == Decimal("100.0000")
        assert InventoryBatch.objects.get(pk=other_batch.pk).quantity_remaining == Decimal("98.0000")
        assert InvoiceItem.objects.get(invoice=sold_invoice).item_id == other.id

    def test_adding_and_removing_lines(self, auth_api, stock, gl_accounts, sold_invoice):
        other = InventoryItemFactory(selling_price=10000)
        InventoryBatchFactory(
            item=other, warehouse=stock["warehouse"],
            quantity_initial=Decimal("100"), quantity_remaining=Decimal("100"),
            value=Decimal("500000"),
        )

        res = auth_api.put(
            reverse("invoice-detail", args=[sold_invoice.pk]),
            {"grand_total": 30000, "items": [
                {"item_id": stock["item"].id, "quantity": 1, "price": 10000},
                {"item_id": other.id, "quantity": 2, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content
        assert InvoiceItem.objects.filter(invoice=sold_invoice).count() == 2

    def test_discount_pct_survives_an_edit(self, auth_api, stock, gl_accounts, sold_invoice):
        res = auth_api.put(
            reverse("invoice-detail", args=[sold_invoice.pk]),
            {"items": [
                {"item_id": stock["item"].id, "quantity": 2, "price": 10000, "discount_pct": "15.00"},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content
        assert InvoiceItem.objects.get(invoice=sold_invoice).discount_pct == Decimal("15.00")

    def test_unknown_item_does_not_commit_scalar_changes(self, auth_api, stock, gl_accounts, sold_invoice):
        res = auth_api.put(
            reverse("invoice-detail", args=[sold_invoice.pk]),
            {"grand_total": 999999, "items": [{"item_id": 999999, "quantity": 1, "price": 1}]},
            format="json",
        )
        assert res.status_code == 400, res.content

        sold_invoice.refresh_from_db()
        assert sold_invoice.grand_total == Decimal("20000.00")
        assert InventoryBatch.objects.get(pk=stock["batch"].pk).quantity_remaining == Decimal("98.0000")


@pytest.mark.django_db
class TestReversalIsValueNeutral:
    def test_create_edit_void_returns_to_baseline(self, auth_api, stock, gl_accounts):
        balances_before = _balances()
        batches_before = _batches()

        res = auth_api.post(
            reverse("invoice-create"),
            _create_payload(stock, gl_accounts, quantity=2, price=10000, grand_total=20000),
            format="json",
        )
        assert res.status_code in (200, 201), res.content
        invoice = Invoice.objects.latest("id")
        _run_journal(auth_api)

        res = auth_api.put(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 70000, "items": [
                {"item_id": stock["item"].id, "quantity": 7, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content

        res = auth_api.delete(reverse("invoice-detail", args=[invoice.pk]))
        assert res.status_code == 200, res.content

        assert _balances() == balances_before
        assert _batches() == batches_before


@pytest.mark.django_db
class TestPackageReversal:
    """Editing lines reverses this invoice's package sales and re-applies them.
    An edit is refused when that would cascade away another invoice's redemption."""

    @pytest.fixture
    def bundle(self, stock, gl_accounts):
        patient = Patient.objects.create(name="Ana")
        treatment = Treatment.objects.create(
            code="LASER", name="Laser", category="Skin", price=Decimal("100000"),
        )
        package = TreatmentPackage.objects.create(
            code="PKG5", name="Laser x5", price=Decimal("450000"),
        )
        TreatmentPackageItem.objects.create(package=package, treatment=treatment, sessions=5)
        package.save()  # re-save so the catalog mirror picks up the items
        package.refresh_from_db()
        return {"patient": patient, "treatment": treatment, "package": package}

    def _sell_package(self, auth_api, stock, gl_accounts, bundle):
        payload = _create_payload(
            stock, gl_accounts,
            item_id=bundle["package"].catalog_item.id,
            quantity=1, price=450000, grand_total=450000,
        )
        payload["patient_no"] = bundle["patient"].patient_no
        res = auth_api.post(reverse("invoice-create"), payload, format="json")
        assert res.status_code in (200, 201), res.content
        return Invoice.objects.latest("id")

    def test_selling_a_package_grants_an_entitlement(self, auth_api, stock, gl_accounts, bundle):
        invoice = self._sell_package(auth_api, stock, gl_accounts, bundle)
        assert PatientPackage.objects.filter(purchased_invoice=invoice).count() == 1

    def test_editing_qty_re_grants_the_right_number_of_packages(self, auth_api, stock, gl_accounts, bundle):
        invoice = self._sell_package(auth_api, stock, gl_accounts, bundle)

        res = auth_api.put(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 900000, "items": [
                {"item_id": bundle["package"].catalog_item.id, "quantity": 2, "price": 450000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content
        # Two entitlements, not three: the original sale was reversed first.
        assert PatientPackage.objects.filter(purchased_invoice=invoice).count() == 2

    def test_removing_the_package_line_revokes_the_entitlement(self, auth_api, stock, gl_accounts, bundle):
        invoice = self._sell_package(auth_api, stock, gl_accounts, bundle)

        res = auth_api.put(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 10000, "items": [
                {"item_id": stock["item"].id, "quantity": 1, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content
        assert PatientPackage.objects.filter(purchased_invoice=invoice).count() == 0

    def test_edit_blocked_when_another_invoice_redeemed_the_package(self, auth_api, stock, gl_accounts, bundle):
        invoice = self._sell_package(auth_api, stock, gl_accounts, bundle)
        pp = PatientPackage.objects.get(purchased_invoice=invoice)

        # A later, separate invoice burns a session from that package.
        other_invoice = Invoice.objects.create(
            datetime=timezone.now(), grand_total=Decimal("0"), patient_no=bundle["patient"],
        )
        PatientPackageRedemption.objects.create(
            patient_package=pp, treatment=bundle["treatment"], invoice=other_invoice,
        )

        res = auth_api.put(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 10000, "items": [
                {"item_id": stock["item"].id, "quantity": 1, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 400, res.content
        assert "redeemed" in str(res.content)

        # Nothing moved: entitlement, redemption and the invoice all intact.
        assert PatientPackage.objects.filter(pk=pp.pk).exists()
        assert PatientPackageRedemption.objects.count() == 1
        invoice.refresh_from_db()
        assert invoice.grand_total == Decimal("450000.00")


@pytest.mark.django_db
class TestServiceMaterialReversal:
    """A service line consumes its treatment's materials; reversing must give
    them back. Before the fix, materials were deducted on sale but never
    restocked on edit or void."""

    @pytest.fixture
    def facial(self, stock, gl_accounts):
        material_item = InventoryItemFactory(selling_price=0)
        material_batch = InventoryBatchFactory(
            item=material_item, warehouse=stock["warehouse"],
            quantity_initial=Decimal("100"), quantity_remaining=Decimal("100"),
            value=Decimal("500000"),
        )
        treatment = Treatment.objects.create(
            code="FACIAL", name="Facial", category="Skin", price=Decimal("50000"),
        )
        TreatmentMaterial.objects.create(
            treatment=treatment, item=material_item, quantity_small=Decimal("5"),
        )
        # Treatment.save() mirrors a service InventoryItem into the catalog.
        treatment.refresh_from_db()
        return {"treatment": treatment, "batch": material_batch}

    def test_void_restocks_treatment_materials(self, auth_api, stock, gl_accounts, facial):
        service_item = facial["treatment"].catalog_item
        assert service_item is not None and service_item.is_service

        res = auth_api.post(
            reverse("invoice-create"),
            _create_payload(stock, gl_accounts, item_id=service_item.id,
                            quantity=1, price=50000, grand_total=50000),
            format="json",
        )
        assert res.status_code in (200, 201), res.content
        invoice = Invoice.objects.latest("id")
        _run_journal(auth_api)

        # 5 units of material consumed by the service.
        assert InventoryBatch.objects.get(pk=facial["batch"].pk).quantity_remaining == Decimal("95.0000")

        res = auth_api.delete(reverse("invoice-detail", args=[invoice.pk]))
        assert res.status_code == 200, res.content

        assert InventoryBatch.objects.get(pk=facial["batch"].pk).quantity_remaining == Decimal("100.0000")

    def test_edit_off_a_service_line_restocks_materials(self, auth_api, stock, gl_accounts, facial):
        service_item = facial["treatment"].catalog_item

        res = auth_api.post(
            reverse("invoice-create"),
            _create_payload(stock, gl_accounts, item_id=service_item.id,
                            quantity=1, price=50000, grand_total=50000),
            format="json",
        )
        assert res.status_code in (200, 201), res.content
        invoice = Invoice.objects.latest("id")
        _run_journal(auth_api)
        assert InventoryBatch.objects.get(pk=facial["batch"].pk).quantity_remaining == Decimal("95.0000")

        # Swap the service for a plain product: the material must come back.
        res = auth_api.put(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 10000, "items": [
                {"item_id": stock["item"].id, "quantity": 1, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content

        assert InventoryBatch.objects.get(pk=facial["batch"].pk).quantity_remaining == Decimal("100.0000")
