"""Regression tests for invoices accumulating duplicate journal postings.

Two independent bugs produced the same symptom — an invoice's detail page listing
the same set of legs over and over:

  1. Every PATCH on a posted invoice wrote an edit-memo reversal + repost pair,
     even when the edit changed nothing the posting is derived from. Net-zero, so
     no balance ever looked wrong, which is why it went unnoticed while the rows
     piled up.
  2. A commit could post a document that was already posted — a double-clicked
     commit button, or two journal runs whose preview phases interleaved. Not
     cosmetic: revenue counted twice and stock consumed twice.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import (
    InventoryBatch, Invoice, JournalStagingBatch, LedgerEntry,
)
from managementsys.services import journal_preview
from managementsys.views.accounting_page import _build_journal_summary


def _payload(stock, gl_accounts, *, quantity=2, price=10000, grand_total=20000):
    return {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash_method"].id,
        "discount": 0, "tax": 0, "additional_charges": 0,
        "grand_total": grand_total,
        "items": [{"item_id": stock["item"].id, "price": price, "quantity": quantity}],
    }


def _sell(auth_api, stock, gl_accounts, **kw):
    res = auth_api.post(reverse("invoice-create"), _payload(stock, gl_accounts, **kw),
                        format="json")
    assert res.status_code in (200, 201), res.content
    return Invoice.objects.latest("id")


def _run_journal(auth_api):
    res = auth_api.post(reverse("accounting-journal-run"),
                        {"date_to": datetime.date.today().isoformat()}, format="json")
    assert res.status_code == 200, res.content


def _rows(invoice):
    return LedgerEntry.objects.filter(invoice=invoice)


def _preview():
    """Stage a draft and return it."""
    batch_id = None
    for evt in journal_preview.build_preview(None, datetime.date.today()):
        if evt["type"] == "done":
            batch_id = evt["staging_batch_id"]
    return JournalStagingBatch.objects.get(pk=batch_id)


@pytest.mark.django_db
class TestNoOpEditWritesNothing:
    def test_patch_that_changes_nothing_adds_no_ledger_rows(
            self, auth_api, stock, gl_accounts):
        invoice = _sell(auth_api, stock, gl_accounts)
        _run_journal(auth_api)
        assert _rows(invoice).count() == 4

        # Exactly what the edit dialog re-submits when the user saves without
        # touching a field.
        for _ in range(3):
            res = auth_api.patch(
                reverse("invoice-detail", args=[invoice.pk]),
                {"grand_total": 20000,
                 "items": [{"item_id": stock["item"].id, "price": 10000, "quantity": 2}]},
                format="json",
            )
            assert res.status_code == 200, res.content

        assert _rows(invoice).count() == 4
        # And no phantom stock churn: three reverse/repost cycles used to restock
        # and re-deduct the batch each time.
        assert InventoryBatch.objects.get(
            pk=stock["batch"].pk).quantity_remaining == Decimal("98.0000")

    def test_a_real_edit_still_writes_the_memo_pair(self, auth_api, stock, gl_accounts):
        invoice = _sell(auth_api, stock, gl_accounts)
        _run_journal(auth_api)

        res = auth_api.patch(
            reverse("invoice-detail", args=[invoice.pk]),
            {"grand_total": 30000,
             "items": [{"item_id": stock["item"].id, "price": 10000, "quantity": 3}]},
            format="json",
        )
        assert res.status_code == 200, res.content

        assert _rows(invoice).filter(source_type="edit_memo").count() == 8
        gl_accounts["cash"].refresh_from_db()
        assert gl_accounts["cash"].balance == Decimal("30000")
        gl_accounts["revenue"].refresh_from_db()
        assert gl_accounts["revenue"].balance == Decimal("30000")

    def test_changing_only_the_payment_account_is_a_real_edit(
            self, auth_api, stock, gl_accounts):
        """The payment leg's account is part of the posting even though no
        amount moves — this must not be mistaken for a no-op."""
        invoice = _sell(auth_api, stock, gl_accounts)
        _run_journal(auth_api)

        res = auth_api.patch(
            reverse("invoice-detail", args=[invoice.pk]),
            {"payment_account_id": gl_accounts["bank"].id},
            format="json",
        )
        assert res.status_code == 200, res.content

        gl_accounts["cash"].refresh_from_db()
        gl_accounts["bank"].refresh_from_db()
        assert gl_accounts["cash"].balance == Decimal("0")
        assert gl_accounts["bank"].balance == Decimal("20000")


@pytest.mark.django_db(transaction=True)
class TestCommitNeverPostsTwice:
    def test_committing_the_same_draft_twice_posts_once(
            self, auth_api, stock, gl_accounts):
        invoice = _sell(auth_api, stock, gl_accounts)
        draft = _preview()
        # Two handles on the same row — a double-clicked commit button. The
        # view's status check cannot separate them: it runs before the
        # generator, and the generator only starts when the stream is consumed.
        again = JournalStagingBatch.objects.get(pk=draft.pk)

        list(journal_preview.commit_preview(None, draft, _build_journal_summary))
        events = list(journal_preview.commit_preview(None, again, _build_journal_summary))

        assert events[-1]["type"] == "error"
        assert _rows(invoice).count() == 4

    def test_interleaved_runs_post_the_document_once(
            self, auth_api, stock, gl_accounts):
        """Two runs whose previews both staged the same still-unposted invoice.

        Both drafts are legitimately claimable and both fingerprints are fresh,
        so only the per-document check stops the second posting.
        """
        invoice = _sell(auth_api, stock, gl_accounts)

        first = _preview()
        # The second preview would normally discard the first; it does not when
        # the first is mid-commit, which is exactly the race.
        second = _preview()
        JournalStagingBatch.objects.filter(pk=first.pk).update(status="draft")

        list(journal_preview.commit_preview(None, first, _build_journal_summary))
        events = list(journal_preview.commit_preview(
            None, JournalStagingBatch.objects.get(pk=second.pk), _build_journal_summary))

        done = events[-1]
        assert done["type"] == "done"
        assert done["documents_posted"] == 0
        assert len(done["skipped"]) == 1

        assert _rows(invoice).count() == 4
        gl_accounts["revenue"].refresh_from_db()
        assert gl_accounts["revenue"].balance == Decimal("20000")
        assert InventoryBatch.objects.get(
            pk=stock["batch"].pk).quantity_remaining == Decimal("98.0000")
