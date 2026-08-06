"""Phase 2: on-demand journal posting, day tracking, and same-day memo entries.

Covers:
  * an unposted Invoice/PurchaseInvoice/AccountTransfer writes zero LedgerEntry
    rows at creation time (the invariant the rest of this phase is built on)
  * a journal run sweeps multiple previously-missed days into one JournalBatch
    and upserts JournalDayLog for every day in between, not just active ones
  * GET journal/status/ reports is_posted per day, including never-swept days
  * voiding a posted invoice writes a same-day reversing memo without
    reopening the original day's JournalDayLog
  * editing a posted invoice writes a same-day reversal + repost memo pair
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    AccountTransfer, Invoice, JournalBatch, JournalDayLog, LedgerEntry,
)


def _create_invoice(auth_api, stock, gl_accounts, *, dt=None, quantity=2, price=10000, grand_total=20000):
    payload = {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash_method"].id,
        "discount": 0, "tax": 0, "additional_charges": 0,
        "grand_total": grand_total,
        "items": [{"item_id": stock["item"].id, "price": price, "quantity": quantity}],
    }
    if dt is not None:
        payload["datetime"] = dt.isoformat()
    res = auth_api.post(reverse("invoice-create"), payload, format="json")
    assert res.status_code in (200, 201), res.content
    return Invoice.objects.get(pk=res.json()["id"])


def _today():
    """The app's "today" — Django's active timezone, not the OS clock.

    Memo entries are dated ``timezone.now().date()``; on a developer machine
    whose OS zone is ahead of ``settings.TIME_ZONE`` (UTC) ``date.today()``
    can already be tomorrow, so asserting against it flakes for hours a day.
    """
    return timezone.localdate()


def _run_journal(auth_api, date_to):
    res = auth_api.post(reverse("accounting-journal-run"), {"date_to": date_to}, format="json")
    assert res.status_code == 200, res.content
    return res.json()


@pytest.mark.django_db
class TestUnpostedCreationHasNoLedgerEffect:
    def test_invoice_creation_writes_zero_ledger_entries(self, auth_api, stock, gl_accounts):
        invoice = _create_invoice(auth_api, stock, gl_accounts)
        assert invoice.posting_status == "unposted"
        assert LedgerEntry.objects.filter(invoice=invoice).count() == 0

    def test_account_transfer_creation_writes_zero_ledger_entries(self, auth_api, gl_accounts):
        res = auth_api.post(reverse("accounting-transfers"), {
            "transfer_date": "2026-08-01",
            "from_account": gl_accounts["cash"].id,
            "to_account": gl_accounts["undeposited"].id,
            "amount": 5000,
            "description": "test transfer",
        }, format="json")
        assert res.status_code == 201, res.content
        transfer = AccountTransfer.objects.latest("id")
        assert transfer.posting_status == "unposted"
        assert LedgerEntry.objects.filter(transfer=transfer).count() == 0
        gl_accounts["cash"].refresh_from_db()
        assert gl_accounts["cash"].balance == Decimal("0")


@pytest.mark.django_db
class TestJournalRunSweepsMissedDays:
    def test_sweeps_multiple_old_unposted_days_into_one_batch(self, auth_api, stock, gl_accounts):
        # Three invoices dated on three different, non-consecutive past days —
        # none of them ever swept. A single run with date_to covering all three
        # must catch every one of them ("sweep up missed days"), not just today.
        d1 = datetime.datetime(2026, 7, 1, 10, 0)
        d2 = datetime.datetime(2026, 7, 15, 10, 0)
        d3 = datetime.datetime(2026, 7, 20, 10, 0)
        inv1 = _create_invoice(auth_api, stock, gl_accounts, dt=d1)
        inv2 = _create_invoice(auth_api, stock, gl_accounts, dt=d2)
        inv3 = _create_invoice(auth_api, stock, gl_accounts, dt=d3)

        result = _run_journal(auth_api, "2026-07-31")

        assert result["status"] == "completed"
        assert result["swept_range_start"] == "2026-07-01"
        assert result["swept_range_end"] == "2026-07-31"
        assert result["documents_posted"] == 3

        for inv in (inv1, inv2, inv3):
            inv.refresh_from_db()
            assert inv.posting_status == "posted"
            assert LedgerEntry.objects.filter(invoice=inv).exists()

        batch = JournalBatch.objects.get(pk=result["batch_id"])
        assert batch.status == "completed"

        # Every calendar day between the earliest posted document and date_to
        # is marked posted, including days with no activity at all.
        no_activity_day = JournalDayLog.objects.get(date=datetime.date(2026, 7, 8))
        assert no_activity_day.is_posted is True
        assert no_activity_day.transaction_count == 0

        active_day = JournalDayLog.objects.get(date=datetime.date(2026, 7, 1))
        assert active_day.is_posted is True
        assert active_day.transaction_count == 1
        assert active_day.batch_id == batch.id

    def test_second_run_does_not_repost_already_posted_documents(self, auth_api, stock, gl_accounts):
        invoice = _create_invoice(auth_api, stock, gl_accounts,
                                   dt=datetime.datetime(2026, 7, 1, 10, 0))
        _run_journal(auth_api, "2026-07-31")
        before = LedgerEntry.objects.filter(invoice=invoice).count()

        result = _run_journal(auth_api, "2026-08-01")
        assert result["documents_posted"] == 0
        assert LedgerEntry.objects.filter(invoice=invoice).count() == before

    def test_journal_status_endpoint_reports_unswept_days_as_unposted(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts, dt=datetime.datetime(2026, 7, 10, 10, 0))
        _run_journal(auth_api, "2026-07-10")

        res = auth_api.get(reverse("accounting-journal-status"),
                            {"date_from": "2026-07-09", "date_to": "2026-07-11"})
        assert res.status_code == 200, res.content
        body = res.json()
        assert body["2026-07-09"] is False   # never swept
        assert body["2026-07-10"] is True    # swept by the run above
        assert body["2026-07-11"] is False   # never swept


@pytest.mark.django_db
class TestVoidMemoOnPostedInvoice:
    def test_void_of_posted_invoice_writes_same_day_reversal_and_leaves_original_day_alone(
        self, auth_api, stock, gl_accounts,
    ):
        invoice = _create_invoice(auth_api, stock, gl_accounts,
                                   dt=datetime.datetime(2026, 7, 1, 10, 0))
        _run_journal(auth_api, "2026-07-01")
        invoice.refresh_from_db()
        assert invoice.posting_status == "posted"

        original_day = JournalDayLog.objects.get(date=datetime.date(2026, 7, 1))
        assert original_day.is_posted is True

        res = auth_api.delete(reverse("invoice-detail", args=[invoice.pk]))
        assert res.status_code == 200, res.content

        # Original day's JournalDayLog is untouched.
        original_day.refresh_from_db()
        assert original_day.is_posted is True

        # A same-day (today) reversing memo exists, tagged accordingly.
        memo_entries = LedgerEntry.objects.filter(invoice=invoice, source_type="void_memo")
        assert memo_entries.exists()
        today = _today()
        assert all(e.date == today for e in memo_entries)

        # The original posting on 2026-07-01 is untouched (still there).
        original_entries = LedgerEntry.objects.filter(invoice=invoice, source_type="invoice")
        assert original_entries.exists()
        assert all(e.date == datetime.date(2026, 7, 1) for e in original_entries)

        # Net balance effect is zero — the memo fully offsets the original posting.
        gl_accounts["cash"].refresh_from_db()
        assert gl_accounts["cash"].balance == Decimal("0")

    def test_void_of_unposted_invoice_writes_no_memo(self, auth_api, stock, gl_accounts):
        invoice = _create_invoice(auth_api, stock, gl_accounts)
        assert invoice.posting_status == "unposted"

        res = auth_api.delete(reverse("invoice-detail", args=[invoice.pk]))
        assert res.status_code == 200, res.content
        assert LedgerEntry.objects.filter(invoice=invoice).count() == 0


@pytest.mark.django_db
class TestEditMemoOnPostedInvoice:
    def test_edit_of_posted_invoice_writes_same_day_reversal_and_repost_pair(
        self, auth_api, stock, gl_accounts,
    ):
        invoice = _create_invoice(auth_api, stock, gl_accounts,
                                   dt=datetime.datetime(2026, 7, 1, 10, 0),
                                   quantity=2, price=10000, grand_total=20000)
        _run_journal(auth_api, "2026-07-01")

        original_day = JournalDayLog.objects.get(date=datetime.date(2026, 7, 1))

        res = auth_api.put(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 50000, "items": [
                {"item_id": stock["item"].id, "quantity": 5, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content

        # Original day untouched.
        original_day.refresh_from_db()
        assert original_day.is_posted is True

        memo_entries = LedgerEntry.objects.filter(invoice=invoice, source_type="edit_memo")
        assert memo_entries.exists()
        today = _today()
        assert all(e.date == today for e in memo_entries)

        # Both a reversal (credit-side undo of the old debit-cash leg) and a
        # fresh posting (new debit-cash leg for the new grand_total) exist.
        cash = gl_accounts["cash"]
        debit_memo_legs = memo_entries.filter(account=cash, entry_type="debit")
        credit_memo_legs = memo_entries.filter(account=cash, entry_type="credit")
        assert debit_memo_legs.exists()
        assert credit_memo_legs.exists()

        cash.refresh_from_db()
        assert cash.balance == Decimal("50000")   # net effect: only the new total

        invoice.refresh_from_db()
        assert invoice.posting_status == "posted"  # still posted, not reopened

    def test_edit_of_unposted_invoice_writes_no_memo(self, auth_api, stock, gl_accounts):
        invoice = _create_invoice(auth_api, stock, gl_accounts)
        assert invoice.posting_status == "unposted"

        res = auth_api.put(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 50000, "items": [
                {"item_id": stock["item"].id, "quantity": 5, "price": 10000},
            ]},
            format="json",
        )
        assert res.status_code == 200, res.content
        assert LedgerEntry.objects.filter(invoice=invoice).count() == 0
        invoice.refresh_from_db()
        assert invoice.posting_status == "unposted"
