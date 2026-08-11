"""A split payment must reach the ledger as one debit leg per method.

Before InvoicePayment existed, the POS collapsed "Rp 200.000 cash + Rp 150.000
BCA" onto whichever method the cashier picked first and wrote the breakdown into
``Invoice.notes`` as free text. The cash drawer was then overstated by the whole
grand total and the bank account never moved — a reconciliation the accountant
could not do without reading prose.

These tests pin the split end to end: the rows survive the create, each account
is debited for its own amount, the entry still balances, an edit re-posts the new
split rather than the old one, and a void unwinds each account separately.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import Invoice, InvoicePayment, LedgerEntry

from .factories import PaymentMethodFactory


def _run_journal(auth_api, through=None):
    date_to = (through or datetime.date.today()).isoformat()
    res = auth_api.post(reverse("accounting-journal-run"), {"date_to": date_to}, format="json")
    assert res.status_code == 200, res.content
    return res.json()


def _sides(invoice):
    entries = LedgerEntry.objects.filter(invoice=invoice)
    debit = sum((e.amount for e in entries if e.entry_type == "debit"), Decimal("0"))
    credit = sum((e.amount for e in entries if e.entry_type == "credit"), Decimal("0"))
    return debit, credit


def _debits_on(invoice, account):
    return sum(
        (e.amount for e in LedgerEntry.objects.filter(
            invoice=invoice, account=account, entry_type="debit")),
        Decimal("0"),
    )


def _credits_on(invoice, account):
    return sum(
        (e.amount for e in LedgerEntry.objects.filter(
            invoice=invoice, account=account, entry_type="credit")),
        Decimal("0"),
    )


@pytest.fixture
def bank_method(gl_accounts):
    return PaymentMethodFactory(name="Bank BCA", linked_account=gl_accounts["bank"])


def _create(auth_api, stock, payload_extra, grand_total=350000):
    payload = {
        "warehouse_id": stock["warehouse"].id,
        "discount": 0, "tax": 0, "additional_charges": 0,
        "grand_total": grand_total,
        "items": [{"item_name": "Light Peel", "price": grand_total, "quantity": 1}],
    }
    payload.update(payload_extra)
    return auth_api.post(reverse("invoice-create"), payload, format="json")


@pytest.mark.django_db
class TestSplitPaymentCreate:
    def test_split_is_stored_as_rows_not_folded_into_one(
            self, auth_api, stock, gl_accounts, bank_method):
        res = _create(auth_api, stock, {"payments": [
            {"payment_method_id": gl_accounts["cash_method"].id, "amount": 200000},
            {"payment_method_id": bank_method.id, "amount": 150000},
        ]})
        assert res.status_code == 201, res.content

        invoice = Invoice.objects.latest("id")
        rows = list(invoice.payments.all())
        assert [r.amount for r in rows] == [Decimal("200000"), Decimal("150000")]
        assert [r.payment_account_id for r in rows] == [
            gl_accounts["cash"].id, gl_accounts["bank"].id]
        # The scalar fields still describe the invoice for every reader that
        # only knows about one method.
        assert invoice.payment_method_id == gl_accounts["cash_method"].id
        assert invoice.payment_account_id == gl_accounts["cash"].id

    def test_each_account_is_debited_for_its_own_amount(
            self, auth_api, stock, gl_accounts, bank_method):
        _create(auth_api, stock, {"payments": [
            {"payment_method_id": gl_accounts["cash_method"].id, "amount": 200000},
            {"payment_method_id": bank_method.id, "amount": 150000},
        ]})
        invoice = Invoice.objects.latest("id")
        _run_journal(auth_api)

        assert _debits_on(invoice, gl_accounts["cash"]) == Decimal("200000")
        assert _debits_on(invoice, gl_accounts["bank"]) == Decimal("150000")
        debit, credit = _sides(invoice)
        assert debit == credit

    def test_split_by_account_id_instead_of_method(self, auth_api, stock, gl_accounts):
        """A caller with no PaymentMethod rows can address the COA directly."""
        res = _create(auth_api, stock, {"payments": [
            {"payment_account_id": gl_accounts["cash"].id, "amount": 100000},
            {"payment_account_id": gl_accounts["bank"].id, "amount": 250000},
        ]})
        assert res.status_code == 201, res.content
        invoice = Invoice.objects.latest("id")
        _run_journal(auth_api)
        assert _debits_on(invoice, gl_accounts["bank"]) == Decimal("250000")

    def test_single_payment_row_leaves_no_split(self, auth_api, stock, gl_accounts):
        """One tender is not a split — it stays on the invoice's own fields."""
        res = _create(auth_api, stock, {"payments": [
            {"payment_method_id": gl_accounts["cash_method"].id, "amount": 350000},
        ]})
        assert res.status_code == 201, res.content
        invoice = Invoice.objects.latest("id")
        assert invoice.payments.count() == 0
        assert invoice.payment_account_id == gl_accounts["cash"].id

    def test_amounts_must_sum_to_grand_total(self, auth_api, stock, gl_accounts, bank_method):
        """Cash *tendered* (with change) would otherwise land in Sales Discount."""
        res = _create(auth_api, stock, {"payments": [
            {"payment_method_id": gl_accounts["cash_method"].id, "amount": 250000},
            {"payment_method_id": bank_method.id, "amount": 150000},
        ]})
        assert res.status_code == 400, res.content
        assert "payments" in res.json()
        assert Invoice.objects.count() == 0

    def test_non_cash_account_is_rejected(self, auth_api, stock, gl_accounts, bank_method):
        res = _create(auth_api, stock, {"payments": [
            {"payment_account_id": gl_accounts["revenue"].id, "amount": 200000},
            {"payment_method_id": bank_method.id, "amount": 150000},
        ]})
        assert res.status_code == 400, res.content

    def test_no_payments_field_behaves_exactly_as_before(
            self, auth_api, stock, gl_accounts):
        res = _create(auth_api, stock, {
            "payment_method_id": gl_accounts["cash_method"].id})
        assert res.status_code == 201, res.content
        invoice = Invoice.objects.latest("id")
        _run_journal(auth_api)
        assert invoice.payments.count() == 0
        assert _debits_on(invoice, gl_accounts["cash"]) == Decimal("350000")


@pytest.mark.django_db
class TestSplitPaymentReporting:
    def test_sales_report_credits_each_method_its_own_share(
            self, auth_api, stock, gl_accounts, bank_method):
        """The report the drawer is counted against must not heap the total on one."""
        _create(auth_api, stock, {"payments": [
            {"payment_method_id": gl_accounts["cash_method"].id, "amount": 200000},
            {"payment_method_id": bank_method.id, "amount": 150000},
        ]})
        # A plain single-method sale alongside it, to pin that path unchanged.
        _create(auth_api, stock,
                {"payment_method_id": gl_accounts["cash_method"].id},
                grand_total=100000)

        res = auth_api.get(reverse("reports-sales"))
        assert res.status_code == 200, res.content
        by_account = {r["account_name"]: r for r in res.json()["by_account"]}

        assert Decimal(by_account["Cash"]["total"]) == Decimal("300000")
        assert Decimal(by_account["Bank BCA"]["total"]) == Decimal("150000")
        assert res.json()["total"] == "450000.00"


@pytest.mark.django_db
class TestSplitPaymentEditAndVoid:
    def _posted_split_invoice(self, auth_api, stock, gl_accounts, bank_method):
        _create(auth_api, stock, {"payments": [
            {"payment_method_id": gl_accounts["cash_method"].id, "amount": 200000},
            {"payment_method_id": bank_method.id, "amount": 150000},
        ]})
        invoice = Invoice.objects.latest("id")
        _run_journal(auth_api)
        invoice.refresh_from_db()
        assert invoice.posting_status == "posted"
        return invoice

    def test_edit_reverses_the_old_split_not_the_new_one(
            self, auth_api, stock, gl_accounts, bank_method):
        invoice = self._posted_split_invoice(auth_api, stock, gl_accounts, bank_method)

        res = auth_api.patch(
            reverse("invoice-detail", args=[invoice.id]),
            {"payments": [
                {"payment_method_id": gl_accounts["cash_method"].id, "amount": 50000},
                {"payment_method_id": bank_method.id, "amount": 300000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content

        assert [r.amount for r in invoice.payments.all()] == [
            Decimal("50000"), Decimal("300000")]
        # Original 200/150 reversed, new 50/300 posted → net per account.
        assert (_debits_on(invoice, gl_accounts["cash"])
                - _credits_on(invoice, gl_accounts["cash"])) == Decimal("50000")
        assert (_debits_on(invoice, gl_accounts["bank"])
                - _credits_on(invoice, gl_accounts["bank"])) == Decimal("300000")
        debit, credit = _sides(invoice)
        assert debit == credit

    def test_clearing_the_split_returns_to_a_single_method(
            self, auth_api, stock, gl_accounts, bank_method):
        invoice = self._posted_split_invoice(auth_api, stock, gl_accounts, bank_method)

        res = auth_api.patch(
            reverse("invoice-detail", args=[invoice.id]),
            {"payments": []}, format="json",
        )
        assert res.status_code == 200, res.content
        assert invoice.payments.count() == 0
        assert (_debits_on(invoice, gl_accounts["bank"])
                - _credits_on(invoice, gl_accounts["bank"])) == Decimal("0")
        assert (_debits_on(invoice, gl_accounts["cash"])
                - _credits_on(invoice, gl_accounts["cash"])) == Decimal("350000")

    def test_void_unwinds_each_account_separately(
            self, auth_api, stock, gl_accounts, bank_method):
        invoice = self._posted_split_invoice(auth_api, stock, gl_accounts, bank_method)

        res = auth_api.delete(reverse("invoice-detail", args=[invoice.id]))
        assert res.status_code == 200, res.content

        assert _credits_on(invoice, gl_accounts["cash"]) == Decimal("200000")
        assert _credits_on(invoice, gl_accounts["bank"]) == Decimal("150000")
        debit, credit = _sides(invoice)
        assert debit == credit
