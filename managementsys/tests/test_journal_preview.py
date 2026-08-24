"""Phase 4: two-phase journal run (preview → review → commit).

Covers the invariants the whole feature rests on:
  * a preview writes nothing to the ledger, moves no document, and consumes no
    stock — running it twice produces the same numbers
  * committing a reviewed draft posts exactly what was staged, grouped under
    JournalEntry headers
  * a source document edited after review blocks the commit rather than posting
    numbers nobody approved
  * FIFO-derived COGS is flagged as an estimate and recomputed at commit
  * a failed day leaves earlier days posted and is resumable
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    Invoice, JournalDayLog, JournalEntry, JournalStagingBatch, LedgerEntry,
    StagedJournalEntry,
)
from managementsys.services import journal_preview


def _create_invoice(auth_api, stock, gl_accounts, *, dt=None, quantity=2,
                    price=10000, grand_total=20000):
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


def _drain(generator):
    """Collect a preview/commit generator's events into a list."""
    return list(generator)


def _terminal(events):
    return events[-1]


def _preview(actor, date_to):
    return _drain(journal_preview.build_preview(actor, date_to))


def _commit(actor, batch):
    from managementsys.views.accounting_page import _build_journal_summary
    return _drain(journal_preview.commit_preview(actor, batch, _build_journal_summary))


@pytest.mark.django_db
class TestPreviewIsSideEffectFree:
    def test_preview_writes_no_ledger_rows_and_moves_no_document(
        self, auth_api, stock, gl_accounts
    ):
        invoice = _create_invoice(auth_api, stock, gl_accounts,
                                  dt=datetime.datetime(2026, 7, 1, 10, 0))
        batch_before = stock["batch"].quantity_remaining

        events = _preview(None, datetime.date(2026, 7, 31))
        done = _terminal(events)

        assert done["type"] == "done"
        assert done["entry_count"] == 1
        # Nothing reached the ledger.
        assert LedgerEntry.objects.count() == 0
        assert JournalEntry.objects.count() == 0
        assert JournalDayLog.objects.count() == 0
        invoice.refresh_from_db()
        assert invoice.posting_status == "unposted"
        # …and no stock was consumed.
        stock["batch"].refresh_from_db()
        assert stock["batch"].quantity_remaining == batch_before

    def test_preview_is_repeatable_and_supersedes_the_previous_draft(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))

        first = _terminal(_preview(None, datetime.date(2026, 7, 31)))
        second = _terminal(_preview(None, datetime.date(2026, 7, 31)))

        assert first["total_debit"] == second["total_debit"]
        assert first["total_credit"] == second["total_credit"]
        assert first["entry_count"] == second["entry_count"]
        # Only one draft is ever open — accounting is a single set of books.
        assert JournalStagingBatch.objects.filter(status="draft").count() == 1
        assert journal_preview.open_draft().id == second["staging_batch_id"]

    def test_staged_entry_is_balanced_and_carries_its_lines(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))

        entry = StagedJournalEntry.objects.get()
        assert entry.is_balanced
        assert entry.total_debit == entry.total_credit
        assert entry.lines.count() >= 2
        assert entry.source_model == "invoice"
        assert entry.source_fingerprint


@pytest.mark.django_db
class TestCommitPostsWhatWasReviewed:
    def test_commit_creates_journal_entries_and_posts_the_documents(
        self, auth_api, stock, gl_accounts
    ):
        invoice = _create_invoice(auth_api, stock, gl_accounts,
                                  dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))
        draft = journal_preview.open_draft()
        staged_debit = draft.total_debit

        done = _terminal(_commit(None, draft))

        assert done["type"] == "done"
        assert done["status"] == "completed"
        entry = JournalEntry.objects.get()
        assert entry.entry_number.startswith("JE-2026-")
        assert entry.total_debit == entry.total_credit
        assert entry.date == datetime.date(2026, 7, 1)
        # Every ledger line now hangs off a header.
        assert LedgerEntry.objects.filter(journal_entry__isnull=True).count() == 0
        assert entry.lines.count() == LedgerEntry.objects.count()

        invoice.refresh_from_db()
        assert invoice.posting_status == "posted"
        assert JournalDayLog.objects.filter(date=datetime.date(2026, 7, 1),
                                            is_posted=True).exists()

        draft.refresh_from_db()
        assert draft.status == "committed"
        # The reviewed total is what actually landed.
        posted = sum(l.amount for l in LedgerEntry.objects.filter(entry_type="debit"))
        assert posted == staged_debit

    def test_commit_consumes_stock_that_the_preview_left_alone(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0), quantity=2)
        before = stock["batch"].quantity_remaining

        _preview(None, datetime.date(2026, 7, 31))
        stock["batch"].refresh_from_db()
        assert stock["batch"].quantity_remaining == before   # preview: untouched

        _commit(None, journal_preview.open_draft())
        stock["batch"].refresh_from_db()
        assert stock["batch"].quantity_remaining == before - 2   # commit: consumed

    def test_cogs_lines_are_flagged_as_estimates_in_the_preview(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))

        entry = StagedJournalEntry.objects.get()
        assert entry.has_estimate
        assert entry.lines.filter(is_estimated=True).exists()
        # Revenue/cash legs are deterministic and must NOT be flagged.
        assert entry.lines.filter(is_estimated=False).exists()

    def test_empty_range_still_records_a_batch(self, auth_api, gl_accounts):
        _preview(None, datetime.date(2026, 7, 31))
        done = _terminal(_commit(None, journal_preview.open_draft()))
        assert done["type"] == "done"
        assert done["documents_posted"] == 0


@pytest.mark.django_db
class TestPreviewSimulatesFifoAcrossDocuments:
    def test_second_invoice_does_not_see_stock_the_first_already_claimed(
        self, auth_api, stock, gl_accounts
    ):
        """Two invoices drawing on one batch must not both book full COGS.

        Without the shared FifoSimulation each dry run would re-read the
        untouched batch row and the preview would overstate COGS — the exact
        bug the simulation exists to prevent.
        """
        on_hand = int(stock["batch"].quantity_remaining)
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0), quantity=on_hand)
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 2, 10, 0), quantity=on_hand)

        _preview(None, datetime.date(2026, 7, 31))

        first, second = StagedJournalEntry.objects.order_by("date")
        first_cogs = sum(l.amount for l in first.lines.filter(is_estimated=True,
                                                              entry_type="debit"))
        second_cogs = sum(l.amount for l in second.lines.filter(is_estimated=True,
                                                                entry_type="debit"))
        # The first invoice drains the batch; the second finds nothing left.
        assert first_cogs > 0
        assert second_cogs == 0


@pytest.mark.django_db
class TestStalenessBlocksTheCommit:
    def test_editing_the_source_document_after_review_refuses_the_commit(
        self, auth_api, stock, gl_accounts
    ):
        invoice = _create_invoice(auth_api, stock, gl_accounts,
                                  dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))
        draft = journal_preview.open_draft()

        # Someone changes the invoice between review and commit.
        Invoice.objects.filter(pk=invoice.pk).update(grand_total=Decimal("999999"))

        events = _commit(None, draft)
        terminal = _terminal(events)

        assert terminal["type"] == "stale"
        assert len(terminal["stale"]) == 1
        assert terminal["stale"][0]["source_label"]
        # Nothing was posted, and the draft is still reviewable.
        assert LedgerEntry.objects.count() == 0
        assert JournalEntry.objects.count() == 0
        draft.refresh_from_db()
        assert draft.status == "draft"

    def test_untouched_documents_pass_the_freshness_check(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))
        assert journal_preview.check_freshness(journal_preview.open_draft()) == []


@pytest.mark.django_db
class TestDraftLifecycle:
    def test_expired_drafts_are_purged(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))
        draft = journal_preview.open_draft()
        JournalStagingBatch.objects.filter(pk=draft.pk).update(
            expires_at=timezone.now() - datetime.timedelta(hours=1))

        journal_preview.purge_expired_drafts()

        assert not JournalStagingBatch.objects.filter(pk=draft.pk).exists()
        assert journal_preview.open_draft() is None
        # Cascade cleared the staged rows with it.
        assert StagedJournalEntry.objects.count() == 0

    def test_expired_draft_is_not_returned_as_open(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))
        JournalStagingBatch.objects.all().update(
            expires_at=timezone.now() - datetime.timedelta(seconds=1))
        assert journal_preview.open_draft() is None


@pytest.mark.django_db
class TestPreviewEndpoints:
    def test_get_preview_returns_204_when_no_draft(self, auth_api):
        res = auth_api.get(reverse("accounting-journal-preview"))
        assert res.status_code == 204

    def test_entries_endpoint_pages_and_filters(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 2, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))

        res = auth_api.get(reverse("accounting-journal-preview-entries"))
        assert res.status_code == 200
        assert res.json()["count"] == 2

        res = auth_api.get(reverse("accounting-journal-preview-entries"),
                           {"date": "2026-07-01"})
        assert res.json()["count"] == 1

        res = auth_api.get(reverse("accounting-journal-preview-entries"),
                           {"source_type": "purchase"})
        assert res.json()["count"] == 0

    def test_day_rollup_flagged_count_matches_the_only_warnings_filter(
        self, auth_api, stock, gl_accounts,
    ):
        """The rail badges a day "N perlu diperiksa" and clicking it filters to
        that day with only_warnings — the two numbers must agree, or the
        operator opens a day the rail promised had problems and finds none."""
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 2, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))

        days = auth_api.get(reverse("accounting-journal-preview")).json()["days"]
        assert days, "the draft should roll up at least one day"

        for day in days:
            assert day["flagged"] <= day["entries"]
            res = auth_api.get(reverse("accounting-journal-preview-entries"),
                               {"date": day["date"], "only_warnings": "1"})
            assert res.json()["count"] == day["flagged"], day["date"]

    def test_staged_entry_detail_returns_lines(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))
        entry = StagedJournalEntry.objects.get()

        res = auth_api.get(reverse("accounting-journal-preview-entry", args=[entry.id]))
        assert res.status_code == 200
        body = res.json()
        assert len(body["lines"]) == entry.lines.count()
        assert body["batch"]["status"] == "draft"

    def test_discard_closes_the_draft(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))

        res = auth_api.post(reverse("accounting-journal-preview-discard"), {}, format="json")
        assert res.status_code == 200
        assert journal_preview.open_draft() is None

    def test_commit_endpoint_rejects_an_already_committed_draft(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 1, 10, 0))
        _preview(None, datetime.date(2026, 7, 31))
        draft = journal_preview.open_draft()
        _commit(None, draft)

        res = auth_api.post(reverse("accounting-journal-preview-commit"),
                            {"staging_batch_id": draft.id}, format="json")
        assert res.status_code == 409


@pytest.mark.django_db
class TestPreviewWindowStart:
    """``date_from`` bounds the sweep's reach backwards.

    Without it a preview picks up stragglers of any age — correct for the
    unattended run, wrong for an operator who asked for one month.
    """

    def test_documents_before_date_from_are_left_alone(self, auth_api, stock, gl_accounts):
        old = _create_invoice(auth_api, stock, gl_accounts,
                              dt=datetime.datetime(2026, 6, 10, 10, 0))
        inside = _create_invoice(auth_api, stock, gl_accounts,
                                 dt=datetime.datetime(2026, 7, 5, 10, 0))

        events = _drain(journal_preview.build_preview(
            None, datetime.date(2026, 7, 31), datetime.date(2026, 7, 1),
        ))
        done = _terminal(events)
        assert done["type"] == "done"
        assert done["document_count"] == 1
        assert done["date_from"] == "2026-07-01"

        batch = JournalStagingBatch.objects.get(pk=done["staging_batch_id"])
        staged_ids = set(batch.entries.values_list("source_id", flat=True))
        assert staged_ids == {inside.pk}
        assert old.pk not in staged_ids

        # The excluded document is untouched, so a later run still catches it.
        old.refresh_from_db()
        assert old.posting_status == "unposted"

    def test_the_day_grid_starts_at_date_from_not_at_the_first_document(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 20, 10, 0))

        events = _drain(journal_preview.build_preview(
            None, datetime.date(2026, 7, 31), datetime.date(2026, 7, 1),
        ))
        start = events[0]
        assert start["type"] == "start"
        assert start["days"][0]["date"] == "2026-07-01"
        assert start["total"] == 31

    def test_commit_marks_every_day_in_the_window_journalled(
        self, auth_api, stock, gl_accounts
    ):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 20, 10, 0))
        done = _terminal(_drain(journal_preview.build_preview(
            None, datetime.date(2026, 7, 31), datetime.date(2026, 7, 1),
        )))
        batch = JournalStagingBatch.objects.get(pk=done["staging_batch_id"])

        _commit(None, batch)

        posted = set(JournalDayLog.objects.filter(is_posted=True)
                     .values_list("date", flat=True))
        assert datetime.date(2026, 7, 1) in posted
        assert datetime.date(2026, 7, 31) in posted
        # Nothing outside the window was marked.
        assert datetime.date(2026, 6, 30) not in posted

    def test_omitting_date_from_still_reaches_back(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 6, 10, 10, 0))
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 5, 10, 0))

        done = _terminal(_preview(None, datetime.date(2026, 7, 31)))
        assert done["document_count"] == 2
        assert done["date_from"] is None


@pytest.mark.django_db
class TestPreviewEndpointWindow:
    def test_post_accepts_date_from(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 6, 10, 10, 0))
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=datetime.datetime(2026, 7, 5, 10, 0))

        res = auth_api.post(
            reverse("accounting-journal-preview"),
            {"date_to": "2026-07-31", "date_from": "2026-07-01"}, format="json",
        )
        assert res.status_code == 200
        b"".join(res.streaming_content)

        batch = JournalStagingBatch.objects.get(status="draft")
        assert batch.date_from == datetime.date(2026, 7, 1)
        assert batch.document_count == 1

    def test_date_from_after_date_to_is_rejected(self, auth_api):
        res = auth_api.post(
            reverse("accounting-journal-preview"),
            {"date_to": "2026-07-01", "date_from": "2026-07-31"}, format="json",
        )
        assert res.status_code == 400

    def test_malformed_date_from_is_rejected(self, auth_api):
        res = auth_api.post(
            reverse("accounting-journal-preview"),
            {"date_to": "2026-07-31", "date_from": "31/07/2026"}, format="json",
        )
        assert res.status_code == 400
