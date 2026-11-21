"""Operator adjustments on a staged journal, line origin tagging, and the
stock warnings that replaced the blanket "HPP perkiraan" review flag.

The invariants:
  * an adjustment is a balanced set — the entry stays balanced however many
    are added or removed, and an unbalanced set is refused whole
  * commit posts the adjustment lines after the rebuilt system lines, tagged
    origin='user' with their author; system lines are origin='system'
  * re-running the preview carries adjustments to the new draft, and says so
    when the document changed in between or dropped out of the window
  * a routine FIFO estimate is not "needs review"; a stock shortfall or a
    zero-cost draw is
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import (
    InventoryBatch, JournalEntry, LedgerEntry, StagedJournalEntry,
)
from managementsys.services import journal_preview

from .test_journal_preview import _commit, _create_invoice, _preview, _terminal

DAY = datetime.datetime(2026, 7, 1, 10, 0)
DATE_TO = datetime.date(2026, 7, 31)


def _adjust_url(entry):
    return reverse("accounting-journal-preview-adjustments", args=[entry.id])


def _remove_url(entry, group):
    return reverse("accounting-journal-preview-adjustment", args=[entry.id, group])


def _lines(gl, amount="1500"):
    """Reclassify part of the sale as a discount — a typical reviewer fix."""
    return [
        {"account": gl["sales_discount"].id, "entry_type": "debit",
         "amount": amount, "description": "Diskon belum dicatat"},
        {"account": gl["revenue"].id, "entry_type": "credit",
         "amount": amount, "description": "Diskon belum dicatat"},
    ]


def _staged_invoice(auth_api, stock, gl):
    _create_invoice(auth_api, stock, gl, dt=DAY)
    _preview(None, DATE_TO)
    return StagedJournalEntry.objects.get()


@pytest.mark.django_db
class TestAddingAdjustments:
    def test_balanced_set_is_added_as_user_lines(self, auth_api, auth_user, stock, gl_accounts):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        debit_before = entry.total_debit
        system_lines = entry.lines.count()

        res = auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)}, format="json")

        assert res.status_code == 201, res.content
        entry.refresh_from_db()
        user_lines = entry.lines.filter(origin="user")
        assert user_lines.count() == 2
        assert entry.lines.filter(origin="system").count() == system_lines
        assert all(l.created_by_id == auth_user.id for l in user_lines)
        # One set, one group id — removable together.
        assert len({l.adjustment_group for l in user_lines}) == 1
        assert entry.total_debit == debit_before + Decimal("1500")
        assert entry.is_balanced and entry.has_adjustment
        # The draft's headline totals follow.
        batch = journal_preview.open_draft()
        assert batch.total_debit == entry.total_debit

        body = res.json()
        assert body["has_adjustment"] is True
        assert {l["origin"] for l in body["lines"]} == {"system", "user"}

    def test_unbalanced_set_is_refused_whole(self, auth_api, stock, gl_accounts):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        lines = _lines(gl_accounts)
        lines[1]["amount"] = "1000"

        res = auth_api.post(_adjust_url(entry), {"lines": lines}, format="json")

        assert res.status_code == 400
        assert not entry.lines.filter(origin="user").exists()

    def test_a_single_line_is_refused(self, auth_api, stock, gl_accounts):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        res = auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)[:1]},
                            format="json")
        assert res.status_code == 400
        assert "minimal dua baris" in res.json()["lines"]["_"]

    def test_removing_a_set_restores_the_entry(self, auth_api, stock, gl_accounts):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        before = (entry.total_debit, entry.total_credit)
        auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)}, format="json")
        group = entry.lines.filter(origin="user").first().adjustment_group

        res = auth_api.delete(_remove_url(entry, group))

        assert res.status_code == 200
        entry.refresh_from_db()
        assert (entry.total_debit, entry.total_credit) == before
        assert not entry.has_adjustment

    def test_system_lines_cannot_be_removed(self, auth_api, stock, gl_accounts):
        """Only user sets carry a group id, so there is nothing to address a
        system line by — and the delete filter is origin='user' regardless."""
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        assert not entry.lines.filter(origin="system", adjustment_group__isnull=False).exists()

    def test_a_committed_draft_cannot_be_adjusted(self, auth_api, stock, gl_accounts):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        _commit(None, journal_preview.open_draft())

        res = auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)}, format="json")

        assert res.status_code == 409


@pytest.mark.django_db
class TestCommitPostsAdjustments:
    def test_adjustment_lines_reach_the_ledger_tagged_as_user(
        self, auth_api, auth_user, stock, gl_accounts,
    ):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)}, format="json")
        entry.refresh_from_db()
        draft = journal_preview.open_draft()

        done = _terminal(_commit(auth_user, draft))

        assert done["type"] == "done"
        assert done["variances"] == []          # staged totals == posted totals
        posted = JournalEntry.objects.get()
        assert posted.is_balanced
        assert posted.total_debit == entry.total_debit

        user = LedgerEntry.objects.filter(origin="user")
        assert user.count() == 2
        assert set(user.values_list("created_by_id", flat=True)) == {auth_user.id}
        assert user.filter(account=gl_accounts["sales_discount"], entry_type="debit",
                           amount=Decimal("1500")).exists()

        system = LedgerEntry.objects.filter(origin="system")
        assert system.exists()
        # A system line's author is whoever ran the commit.
        assert set(system.values_list("created_by_id", flat=True)) == {auth_user.id}

    def test_entries_without_adjustments_post_only_system_lines(
        self, auth_api, stock, gl_accounts,
    ):
        _staged_invoice(auth_api, stock, gl_accounts)
        _commit(None, journal_preview.open_draft())
        assert not LedgerEntry.objects.filter(origin="user").exists()


@pytest.mark.django_db
class TestRePreviewCarriesAdjustments:
    def test_adjustment_survives_a_re_preview(self, auth_api, stock, gl_accounts):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)}, format="json")
        entry.refresh_from_db()

        done = _terminal(_preview(None, DATE_TO))

        assert done["adjustments_dropped"] == []
        fresh = StagedJournalEntry.objects.get(batch_id=done["staging_batch_id"])
        assert fresh.lines.filter(origin="user").count() == 2
        assert fresh.has_adjustment
        assert fresh.total_debit == entry.total_debit
        assert not any("draf sebelumnya" in w for w in fresh.warnings)

    def test_carried_adjustment_is_flagged_when_the_document_changed(
        self, auth_api, stock, gl_accounts,
    ):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)}, format="json")
        # Stand-in for an edit to the invoice: the fingerprint no longer matches.
        StagedJournalEntry.objects.filter(pk=entry.pk).update(source_fingerprint="changed")

        done = _terminal(_preview(None, DATE_TO))

        fresh = StagedJournalEntry.objects.get(batch_id=done["staging_batch_id"])
        assert fresh.lines.filter(origin="user").count() == 2
        assert any("draf sebelumnya" in w for w in fresh.warnings)

    def test_document_leaving_the_window_is_reported(self, auth_api, stock, gl_accounts):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        auth_api.post(_adjust_url(entry), {"lines": _lines(gl_accounts)}, format="json")

        done = _terminal(_preview(None, datetime.date(2026, 6, 30)))

        assert done["adjustments_dropped"] == [entry.source_label]


@pytest.mark.django_db
class TestStockWarnings:
    def test_routine_fifo_estimate_is_not_flagged_for_review(
        self, auth_api, stock, gl_accounts,
    ):
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        assert entry.has_estimate            # still labelled on the line…
        assert entry.warnings == []          # …but not a problem
        res = auth_api.get(reverse("accounting-journal-preview-entries"),
                           {"only_warnings": "1"})
        assert res.json()["count"] == 0

    def test_selling_more_than_is_in_stock_warns(self, auth_api, stock, gl_accounts):
        on_hand = int(stock["batch"].quantity_remaining)
        _create_invoice(auth_api, stock, gl_accounts, dt=DAY,
                        quantity=on_hand + 5, grand_total=(on_hand + 5) * 10000)
        _preview(None, DATE_TO)

        entry = StagedJournalEntry.objects.get()
        shortfall = [w for w in entry.warnings if w.startswith("Stok kurang")]
        assert len(shortfall) == 1
        assert "HPP untuk 5 tidak dijurnal" in shortfall[0]

    def test_drawing_from_a_zero_value_batch_warns(self, auth_api, stock, gl_accounts):
        InventoryBatch.objects.filter(pk=stock["batch"].pk).update(value=0)
        entry = _staged_invoice(auth_api, stock, gl_accounts)
        assert any(w.startswith("HPP nol") for w in entry.warnings)


@pytest.mark.django_db
class TestOriginOnOtherUserPaths:
    def test_correction_lines_are_user_and_its_reversal_is_system(
        self, auth_api, auth_user, stock, gl_accounts,
    ):
        _staged_invoice(auth_api, stock, gl_accounts)
        _commit(None, journal_preview.open_draft())
        original = JournalEntry.objects.get()

        res = auth_api.post(
            reverse("accounting-journal-entry-correct", args=[original.id]),
            {"memo": "Koreksi", "lines": _lines(gl_accounts)}, format="json",
        )
        assert res.status_code == 201, res.content

        reversal = JournalEntry.objects.get(source_type="reversal")
        correction = JournalEntry.objects.get(source_type="correction")
        assert set(reversal.lines.values_list("origin", flat=True)) == {"system"}
        assert set(correction.lines.values_list("origin", flat=True)) == {"user"}
        assert set(correction.lines.values_list("created_by_id", flat=True)) == {auth_user.id}


@pytest.mark.django_db
def test_migration_0121_backfills_origin_and_author(auth_api, auth_user, stock, gl_accounts):
    """The suite runs --nomigrations, so call the backfill directly against
    rows made to look like they predate the columns."""
    import importlib

    from django.apps import apps

    migration = importlib.import_module("managementsys.migrations.0121_journal_line_origin")

    _staged_invoice(auth_api, stock, gl_accounts)
    _commit(auth_user, journal_preview.open_draft())
    original = JournalEntry.objects.get()
    auth_api.post(reverse("accounting-journal-entry-correct", args=[original.id]),
                  {"memo": "Koreksi", "lines": _lines(gl_accounts)}, format="json")
    # Pretend these were written before 0121: every line 'system', no author.
    LedgerEntry.objects.update(origin="system", created_by=None)

    migration.backfill_origin(apps, None)

    assert set(LedgerEntry.objects.filter(journal_entry__source_type="correction")
               .values_list("origin", flat=True)) == {"user"}
    assert set(LedgerEntry.objects.exclude(journal_entry__source_type="correction")
               .values_list("origin", flat=True)) == {"system"}
    assert not LedgerEntry.objects.filter(created_by__isnull=True).exists()
