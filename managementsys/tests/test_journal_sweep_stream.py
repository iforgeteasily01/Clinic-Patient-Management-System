"""Per-day journal sweep and its SSE stream.

The sweep used to run inside one ``@transaction.atomic`` block covering the
whole run. It now commits one calendar day at a time so the web UI can animate
progress truthfully. These tests pin down that change:

  * the generator emits start / day* / done in order, with 'skipped' for days
    that carried no documents
  * A FAILURE MID-RUN LEAVES EARLIER DAYS COMMITTED. This is the behavioural
    change; if someone re-adds @transaction.atomic to JournalRunView, that
    decorator nests every per-day transaction inside one outer transaction and
    this test is what catches it.
  * the non-streaming endpoint's response is unchanged
  * the stream endpoint emits well-formed text/event-stream frames

See docs/JOURNAL-RUN-PROGRESS-PLAN.md.
"""
import datetime
import json

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import Invoice, JournalBatch, JournalDayLog
from managementsys.services.journal_sweep import run_journal_sweep


def _today():
    return timezone.localdate()


def _create_invoice(auth_api, stock, gl_accounts, *, dt):
    payload = {
        "warehouse_id": stock["warehouse"].id,
        "payment_method_id": gl_accounts["cash_method"].id,
        "discount": 0, "tax": 0, "additional_charges": 0,
        "grand_total": 20000,
        "items": [{"item_id": stock["item"].id, "price": 10000, "quantity": 2}],
        "datetime": dt.isoformat(),
    }
    res = auth_api.post(reverse("invoice-create"), payload, format="json")
    assert res.status_code in (200, 201), res.content
    return Invoice.objects.get(pk=res.json()["id"])


def _drain(posters=None, summary_builder=None, date_to=None, actor=None):
    """Run the generator to completion, returning the list of events."""
    from managementsys.views.accounting_page import _RUN_POSTERS, _build_journal_summary
    return list(run_journal_sweep(
        actor,
        date_to or _today(),
        summary_builder or _build_journal_summary,
        posters if posters is not None else _RUN_POSTERS,
    ))


@pytest.mark.django_db
class TestSweepGenerator:
    def test_emits_start_days_and_done_in_order(self, auth_api, stock, gl_accounts):
        today = _today()
        # Documents on day -2 and today; day -1 deliberately left empty.
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=timezone.now() - datetime.timedelta(days=2))
        _create_invoice(auth_api, stock, gl_accounts, dt=timezone.now())

        events = _drain(date_to=today)

        assert events[0]["type"] == "start"
        assert events[-1]["type"] == "done"

        days = [e for e in events if e["type"] == "day"]
        assert [d["date"] for d in days] == [
            (today - datetime.timedelta(days=2)).isoformat(),
            (today - datetime.timedelta(days=1)).isoformat(),
            today.isoformat(),
        ]
        # The middle day had no documents.
        assert [d["status"] for d in days] == ["posted", "skipped", "posted"]

        # start announces the full day list up front so the grid can render
        # every cell before the first one lights up.
        assert events[0]["total"] == 3
        assert len(events[0]["days"]) == 3

    def test_skipped_days_are_still_marked_posted(self, auth_api, stock, gl_accounts):
        """'skipped' is a display distinction only — the stored state matches
        what the original all-at-once code wrote."""
        today = _today()
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=timezone.now() - datetime.timedelta(days=2))

        _drain(date_to=today)

        empty_day = today - datetime.timedelta(days=1)
        assert JournalDayLog.objects.get(date=empty_day).is_posted is True

    def test_empty_sweep_emits_start_then_done(self, db):
        events = _drain(date_to=_today())
        assert [e["type"] for e in events] == ["start", "done"]
        assert events[0]["days"] == []
        assert events[0]["total"] == 0
        assert events[1]["documents_posted"] == 0
        assert events[1]["summary"]["total_revenue"] == "0.00"


@pytest.mark.django_db
class TestPerDayCommitBoundary:
    """The behavioural change. Read the module docstring."""

    def test_failure_leaves_earlier_days_committed(self, auth_api, stock, gl_accounts):
        today = _today()
        day_a = today - datetime.timedelta(days=2)
        day_b = today - datetime.timedelta(days=1)

        _create_invoice(auth_api, stock, gl_accounts,
                        dt=timezone.now() - datetime.timedelta(days=2))
        _create_invoice(auth_api, stock, gl_accounts,
                        dt=timezone.now() - datetime.timedelta(days=1))
        _create_invoice(auth_api, stock, gl_accounts, dt=timezone.now())

        from managementsys.views.accounting_page import _RUN_POSTERS

        # Blow up only on the last day.
        def exploding_invoice_poster(invoice):
            if invoice.datetime.date() == today:
                raise RuntimeError("boom on the final day")
            _RUN_POSTERS["invoice"](invoice)

        posters = dict(_RUN_POSTERS, invoice=exploding_invoice_poster)
        events = _drain(posters=posters, date_to=today)

        # Terminal event is an error carrying the committed count.
        assert events[-1]["type"] == "error"
        assert events[-1]["date"] == today.isoformat()
        assert events[-1]["days_committed"] == 2
        assert "boom" in events[-1]["message"]

        # THE ASSERTION THAT MATTERS: earlier days survived the exception.
        assert JournalDayLog.objects.filter(date=day_a, is_posted=True).exists()
        assert JournalDayLog.objects.filter(date=day_b, is_posted=True).exists()
        # The failing day did not.
        assert not JournalDayLog.objects.filter(date=today).exists()

        batch = JournalBatch.objects.latest("id")
        assert batch.status == "failed"

    def test_failing_day_is_atomic_within_itself(self, auth_api, stock, gl_accounts):
        """Documents inside one day still roll back together."""
        today = _today()
        _create_invoice(auth_api, stock, gl_accounts, dt=timezone.now())
        _create_invoice(auth_api, stock, gl_accounts, dt=timezone.now())

        from managementsys.views.accounting_page import _RUN_POSTERS

        seen = {"n": 0}

        def poster(invoice):
            seen["n"] += 1
            if seen["n"] == 2:
                raise RuntimeError("second document fails")
            _RUN_POSTERS["invoice"](invoice)

        events = _drain(posters=dict(_RUN_POSTERS, invoice=poster), date_to=today)

        assert events[-1]["type"] == "error"
        # The first document's posting was rolled back with the day.
        assert not JournalDayLog.objects.filter(date=today).exists()
        assert Invoice.objects.filter(posting_status="posted").count() == 0


@pytest.mark.django_db
class TestEndpointContracts:
    def test_non_streaming_response_shape_is_unchanged(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts, dt=timezone.now())

        res = auth_api.post(
            reverse("accounting-journal-run"),
            {"date_to": _today().isoformat()}, format="json",
        )
        assert res.status_code == 200, res.content
        body = res.json()

        assert set(body) == {
            "batch_id", "status", "requested_range_start", "requested_range_end",
            "swept_range_start", "swept_range_end", "documents_posted", "summary",
        }
        assert body["status"] == "completed"
        assert "type" not in body  # the generator's discriminator must be stripped

    def test_missing_date_to_is_a_400_on_both_endpoints(self, auth_api):
        for name in ("accounting-journal-run", "accounting-journal-run-stream"):
            res = auth_api.post(reverse(name), {}, format="json")
            assert res.status_code == 400, (name, res.content)

    def test_malformed_date_to_is_a_400_on_both_endpoints(self, auth_api):
        for name in ("accounting-journal-run", "accounting-journal-run-stream"):
            res = auth_api.post(reverse(name), {"date_to": "not-a-date"}, format="json")
            assert res.status_code == 400, (name, res.content)

    def test_stream_emits_well_formed_sse_frames(self, auth_api, stock, gl_accounts):
        _create_invoice(auth_api, stock, gl_accounts, dt=timezone.now())

        res = auth_api.post(
            reverse("accounting-journal-run-stream"),
            {"date_to": _today().isoformat()}, format="json",
        )
        assert res.status_code == 200
        assert res["Content-Type"].startswith("text/event-stream")
        assert res["Cache-Control"] == "no-cache"
        assert res["X-Accel-Buffering"] == "no"

        raw = b"".join(res.streaming_content).decode()
        frames = [f for f in raw.split("\n\n") if f.strip()]

        parsed = []
        for frame in frames:
            lines = frame.split("\n")
            event = next(l[len("event: "):] for l in lines if l.startswith("event: "))
            data = json.loads(next(l[len("data: "):] for l in lines if l.startswith("data: ")))
            parsed.append((event, data))

        assert parsed[0][0] == "start"
        assert parsed[-1][0] == "done"
        # The event name and the payload discriminator must agree.
        for event, data in parsed:
            assert data["type"] == event
