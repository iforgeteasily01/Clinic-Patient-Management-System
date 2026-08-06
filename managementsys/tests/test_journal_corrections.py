"""Phase 4: correction journals.

A correction never rewrites history. It posts two new entries dated today — an
auto-generated full reversal of the original, and the operator's replacement —
and links both back to the entry being corrected.

Covers:
  * the reversal exactly negates the original, account for account
  * original + reversal net to zero, so only the correction's effect remains
  * both new entries are dated today and link back to the original
  * an entry can only be corrected once, and a reversal cannot be corrected
  * unbalanced / malformed lines are rejected with a per-line error map
  * JournalDayLog is deliberately untouched, so the reports guard still knows
    today has not been swept
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    ChartOfAccounts, Invoice, JournalDayLog, JournalEntry, LedgerEntry,
)
from managementsys.services import journal_preview


def _create_invoice(auth_api, stock, gl_accounts, *, dt=None):
    payload = {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash_method"].id,
        "discount": 0, "tax": 0, "additional_charges": 0,
        "grand_total": 20000,
        "items": [{"item_id": stock["item"].id, "price": 10000, "quantity": 2}],
    }
    if dt is not None:
        payload["datetime"] = dt.isoformat()
    res = auth_api.post(reverse("invoice-create"), payload, format="json")
    assert res.status_code in (200, 201), res.content
    return Invoice.objects.get(pk=res.json()["id"])


def _post_one_invoice(auth_api, stock, gl_accounts):
    """Create an invoice and get it into the ledger via preview + commit."""
    from managementsys.views.accounting_page import _build_journal_summary

    _create_invoice(auth_api, stock, gl_accounts, dt=datetime.datetime(2026, 7, 1, 10, 0))
    list(journal_preview.build_preview(None, datetime.date(2026, 7, 31)))
    list(journal_preview.commit_preview(None, journal_preview.open_draft(),
                                        _build_journal_summary))
    return JournalEntry.objects.get(source_type="invoice")


def _balances():
    return {a.pk: a.balance for a in ChartOfAccounts.objects.all()}


def _today():
    """The app's today, not the OS clock — see test_journal_engine."""
    return timezone.localdate()


@pytest.mark.django_db
class TestCorrectionPostsReversalPlusReplacement:
    def test_creates_a_linked_reversal_and_correction_dated_today(
        self, auth_api, stock, gl_accounts
    ):
        original = _post_one_invoice(auth_api, stock, gl_accounts)

        res = auth_api.post(
            reverse("accounting-journal-entry-correct", args=[original.id]),
            {
                "memo": "Koreksi salah akun",
                "reason": "Pendapatan dicatat ke akun yang salah",
                "lines": [
                    {"account": gl_accounts["cash"].id, "entry_type": "debit",
                     "amount": "15000", "description": "Kas terkoreksi"},
                    {"account": gl_accounts["revenue"].id, "entry_type": "credit",
                     "amount": "15000", "description": "Pendapatan terkoreksi"},
                ],
            },
            format="json",
        )
        assert res.status_code == 201, res.content
        body = res.json()

        reversal = JournalEntry.objects.get(pk=body["reversal"]["id"])
        correction = JournalEntry.objects.get(pk=body["correction"]["id"])

        assert reversal.source_type == "reversal"
        assert correction.source_type == "correction"
        assert reversal.reverses_id == original.id
        assert correction.corrects_id == original.id
        # Both land today; the original day's books are untouched.
        assert reversal.date == _today()
        assert correction.date == _today()
        original.refresh_from_db()
        assert original.date == datetime.date(2026, 7, 1)

    def test_reversal_exactly_negates_the_original(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        original_lines = {
            (l.account_id, l.entry_type): l.amount
            for l in original.lines.all()
        }

        res = auth_api.post(
            reverse("accounting-journal-entry-correct", args=[original.id]),
            {
                "memo": "Koreksi",
                "lines": [
                    {"account": gl_accounts["cash"].id, "entry_type": "debit",
                     "amount": "1", "description": "x"},
                    {"account": gl_accounts["revenue"].id, "entry_type": "credit",
                     "amount": "1", "description": "y"},
                ],
            },
            format="json",
        )
        reversal = JournalEntry.objects.get(pk=res.json()["reversal"]["id"])

        flip = {"debit": "credit", "credit": "debit"}
        reversal_lines = {
            (l.account_id, l.entry_type): l.amount for l in reversal.lines.all()
        }
        assert len(reversal_lines) == len(original_lines)
        for (account_id, side), amount in original_lines.items():
            assert reversal_lines[(account_id, flip[side])] == amount

    def test_original_plus_reversal_net_to_zero_per_account(
        self, auth_api, stock, gl_accounts
    ):
        """The point of a reversal: after it, only the correction's effect is left."""
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        before = _balances()

        auth_api.post(
            reverse("accounting-journal-entry-correct", args=[original.id]),
            {
                "memo": "Koreksi",
                # A correction identical to the original leaves balances where
                # they started — reversal and replacement cancel out.
                "lines": [
                    {"account": l.account_id, "entry_type": l.entry_type,
                     "amount": str(l.amount), "description": l.description}
                    for l in original.lines.all()
                ],
            },
            format="json",
        )

        after = _balances()
        for account_id, balance in before.items():
            assert after[account_id] == balance, f"account {account_id} drifted"

    def test_correction_inherits_the_source_document_link(
        self, auth_api, stock, gl_accounts
    ):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        res = auth_api.post(
            reverse("accounting-journal-entry-correct", args=[original.id]),
            {"memo": "Koreksi", "lines": [
                {"account": gl_accounts["cash"].id, "entry_type": "debit",
                 "amount": "100", "description": "a"},
                {"account": gl_accounts["revenue"].id, "entry_type": "credit",
                 "amount": "100", "description": "b"},
            ]},
            format="json",
        )
        correction = JournalEntry.objects.get(pk=res.json()["correction"]["id"])
        assert correction.invoice_id == original.invoice_id


@pytest.mark.django_db
class TestCorrectionDoesNotDisturbDayTracking:
    def test_journal_day_log_is_left_alone(self, auth_api, stock, gl_accounts):
        """A correction must not mark today as swept.

        Reversal/correction rows are already-posted by construction, the same
        convention the void/edit memo path uses. If this marked today
        is_posted=True the financial-reports guard would let a report through
        for a day whose documents have never been journalled.
        """
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        before = set(JournalDayLog.objects.values_list("date", "is_posted"))

        auth_api.post(
            reverse("accounting-journal-entry-correct", args=[original.id]),
            {"memo": "Koreksi", "lines": [
                {"account": gl_accounts["cash"].id, "entry_type": "debit",
                 "amount": "100", "description": "a"},
                {"account": gl_accounts["revenue"].id, "entry_type": "credit",
                 "amount": "100", "description": "b"},
            ]},
            format="json",
        )

        assert set(JournalDayLog.objects.values_list("date", "is_posted")) == before


@pytest.mark.django_db
class TestCorrectionGuards:
    def _correct(self, auth_api, entry_id, lines, memo="Koreksi"):
        return auth_api.post(
            reverse("accounting-journal-entry-correct", args=[entry_id]),
            {"memo": memo, "lines": lines}, format="json",
        )

    def _valid_lines(self, gl_accounts):
        return [
            {"account": gl_accounts["cash"].id, "entry_type": "debit",
             "amount": "100", "description": "a"},
            {"account": gl_accounts["revenue"].id, "entry_type": "credit",
             "amount": "100", "description": "b"},
        ]

    def test_an_entry_cannot_be_corrected_twice(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        assert self._correct(auth_api, original.id,
                             self._valid_lines(gl_accounts)).status_code == 201
        second = self._correct(auth_api, original.id, self._valid_lines(gl_accounts))
        assert second.status_code == 409
        assert "dibalik" in second.json()["error"]

    def test_a_reversal_cannot_itself_be_corrected(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        res = self._correct(auth_api, original.id, self._valid_lines(gl_accounts))
        reversal_id = res.json()["reversal"]["id"]

        blocked = self._correct(auth_api, reversal_id, self._valid_lines(gl_accounts))
        assert blocked.status_code == 409

    def test_unbalanced_lines_are_rejected(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        res = self._correct(auth_api, original.id, [
            {"account": gl_accounts["cash"].id, "entry_type": "debit",
             "amount": "100", "description": "a"},
            {"account": gl_accounts["revenue"].id, "entry_type": "credit",
             "amount": "90", "description": "b"},
        ])
        assert res.status_code == 400
        assert "seimbang" in res.json()["lines"]["_"]
        assert JournalEntry.objects.filter(source_type="correction").count() == 0

    def test_single_line_entry_is_rejected(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        res = self._correct(auth_api, original.id, [
            {"account": gl_accounts["cash"].id, "entry_type": "debit",
             "amount": "100", "description": "a"},
        ])
        assert res.status_code == 400

    def test_head_accounts_are_rejected(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        head = ChartOfAccounts.objects.filter(is_head=True).first()
        res = self._correct(auth_api, original.id, [
            {"account": head.id, "entry_type": "debit",
             "amount": "100", "description": "a"},
            {"account": gl_accounts["revenue"].id, "entry_type": "credit",
             "amount": "100", "description": "b"},
        ])
        assert res.status_code == 400
        assert "induk" in res.json()["lines"]["0"] or "induk" in res.json()["lines"][0]

    def test_zero_amount_is_rejected(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        res = self._correct(auth_api, original.id, [
            {"account": gl_accounts["cash"].id, "entry_type": "debit",
             "amount": "0", "description": "a"},
            {"account": gl_accounts["revenue"].id, "entry_type": "credit",
             "amount": "0", "description": "b"},
        ])
        assert res.status_code == 400

    def test_missing_memo_is_rejected(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        res = self._correct(auth_api, original.id,
                            self._valid_lines(gl_accounts), memo="")
        assert res.status_code == 400

    def test_nothing_is_written_when_validation_fails(self, auth_api, stock, gl_accounts):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        ledger_before = LedgerEntry.objects.count()
        entries_before = JournalEntry.objects.count()

        self._correct(auth_api, original.id, [
            {"account": gl_accounts["cash"].id, "entry_type": "debit",
             "amount": "100", "description": "a"},
            {"account": gl_accounts["revenue"].id, "entry_type": "credit",
             "amount": "90", "description": "b"},
        ])

        assert LedgerEntry.objects.count() == ledger_before
        assert JournalEntry.objects.count() == entries_before


@pytest.mark.django_db
class TestEntryEndpoints:
    def test_entry_detail_exposes_lines_and_correction_chain(
        self, auth_api, stock, gl_accounts
    ):
        original = _post_one_invoice(auth_api, stock, gl_accounts)

        res = auth_api.get(reverse("accounting-journal-entry", args=[original.id]))
        assert res.status_code == 200
        body = res.json()
        assert body["entry_number"] == original.entry_number
        assert len(body["lines"]) == original.lines.count()
        assert body["source_ref"]["kind"] == "invoice"
        assert body["can_correct"] is True
        assert body["reversed_by"] == []

        auth_api.post(
            reverse("accounting-journal-entry-correct", args=[original.id]),
            {"memo": "Koreksi", "lines": [
                {"account": gl_accounts["cash"].id, "entry_type": "debit",
                 "amount": "100", "description": "a"},
                {"account": gl_accounts["revenue"].id, "entry_type": "credit",
                 "amount": "100", "description": "b"},
            ]}, format="json",
        )

        after = auth_api.get(reverse("accounting-journal-entry", args=[original.id])).json()
        assert len(after["reversed_by"]) == 1
        assert len(after["corrections"]) == 1
        assert after["can_correct"] is False

    def test_correction_draft_prefills_the_original_lines(
        self, auth_api, stock, gl_accounts
    ):
        original = _post_one_invoice(auth_api, stock, gl_accounts)
        res = auth_api.get(
            reverse("accounting-journal-entry-correction-draft", args=[original.id]))
        assert res.status_code == 200
        body = res.json()
        assert body["blocked_reason"] is None
        assert len(body["lines"]) == original.lines.count()
        assert body["correction_date"] == _today().isoformat()

    def test_entry_list_filters_and_annotates(self, auth_api, stock, gl_accounts):
        _post_one_invoice(auth_api, stock, gl_accounts)

        res = auth_api.get(reverse("accounting-journal-entries"))
        assert res.status_code == 200
        body = res.json()
        assert body["count"] == 1
        row = body["results"][0]
        assert row["line_count"] > 0
        assert row["is_reversed"] is False

        filtered = auth_api.get(reverse("accounting-journal-entries"),
                                {"source_type": "expense"}).json()
        assert filtered["count"] == 0

    def test_ledger_lines_expose_their_entry_number(self, auth_api, stock, gl_accounts):
        entry = _post_one_invoice(auth_api, stock, gl_accounts)
        res = auth_api.get(reverse("accounting-journal"))
        assert res.status_code == 200
        rows = res.json()["results"]
        assert rows
        assert all(r["entry_number"] == entry.entry_number for r in rows)
        assert all(r["journal_entry_id"] == entry.id for r in rows)
