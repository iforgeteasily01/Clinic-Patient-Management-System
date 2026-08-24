"""Journal sweep, expressed as a per-day generator.

Extracted from ``JournalRunView`` so that the same code can serve both the
original single-response endpoint and the SSE streaming endpoint that drives the
live per-day animation in the web UI.

TRANSACTION BOUNDARIES — READ THIS
==================================
The sweep used to run inside one ``@transaction.atomic`` block covering every
day. It no longer does: **each calendar day commits in its own transaction.**

Consequence: if day *k* raises, days 1…*k*-1 remain posted and are NOT rolled
back. The batch is marked ``failed`` and re-running resumes from day *k*.

This is deliberate. A sweep catches documents of arbitrary age, so under the old
behaviour a single malformed invoice from three weeks ago blocked posting for
every other day, and each retry redid all the work. Per-day commits make the
operation resumable.

The trade-off is that a failure now leaves a partially-posted range, so the UI
MUST tell the operator how many days were committed. See
``docs/JOURNAL-RUN-PROGRESS-PLAN.md`` (JS-F).

Documents within a single day are still atomic together.
"""

import datetime

from django.db import transaction
from django.utils import timezone

from ..models import (
    AccountTransfer, Expense, Invoice, JournalBatch, JournalDayLog,
    PurchaseInvoice, SalesReturn, StockOutLog,
)

# Reasons whose cost reaches the P&L. Derived from the model's own mapping so
# the two can never drift: a reason mapped to None (a warehouse transfer) moves
# stock between locations the clinic still owns and has no journal at all.
JOURNALABLE_STOCK_REASONS = [
    reason for reason, account in StockOutLog.REASON_ACCOUNTS.items() if account
]


def _gather_events(date_to, date_from=None):
    """Every unposted document dated on or before ``date_to``.

    Returns ``{date: [(kind, obj), ...]}``. Same four querysets the original
    view used — unchanged, including the ``is_voided`` exclusions — plus stock
    corrections from Phase 5. Same-day void/edit memo entries are never selected
    here: they carry source_type='void_memo'/'edit_memo' and are written
    already-posted by the void/edit endpoints.

    ``date_from``, when given, is a hard lower bound: a document dated before
    it is left alone even if unposted. Without it the sweep reaches back as
    far as there are stragglers, which is the historical "sweep" behaviour.

    Only stock corrections that will actually produce legs are gathered — a
    warehouse transfer moves no value and a zero-cost issue has nothing to
    charge. Those rows are stamped 'posted' the moment they are written (see
    ``StockOutView``), so filtering here keeps the preview pure: it never has to
    mutate a row just to stop reconsidering it.
    """
    by_date = {}

    def add(day, kind, obj):
        by_date.setdefault(day, []).append((kind, obj))

    inv_qs = Invoice.objects.filter(
        posting_status='unposted', is_voided=False, datetime__date__lte=date_to,
    )
    if date_from:
        inv_qs = inv_qs.filter(datetime__date__gte=date_from)
    for inv in inv_qs:
        add(inv.datetime.date(), 'invoice', inv)

    purchase_qs = PurchaseInvoice.objects.filter(
        posting_status='unposted', is_voided=False, purchase_date__lte=date_to,
    ).select_related('supplier')
    if date_from:
        purchase_qs = purchase_qs.filter(purchase_date__gte=date_from)
    for p in purchase_qs:
        add(p.purchase_date, 'purchase', p)

    transfer_qs = AccountTransfer.objects.filter(
        posting_status='unposted', transfer_date__lte=date_to,
    )
    if date_from:
        transfer_qs = transfer_qs.filter(transfer_date__gte=date_from)
    for t in transfer_qs:
        add(t.transfer_date, 'transfer', t)

    expense_qs = Expense.objects.filter(
        posting_status='unposted', expense_date__lte=date_to,
    )
    if date_from:
        expense_qs = expense_qs.filter(expense_date__gte=date_from)
    for e in expense_qs:
        add(e.expense_date, 'expense', e)

    stock_qs = StockOutLog.objects.filter(
        posting_status='unposted', out_date__lte=date_to,
        value__gt=0, reason__in=JOURNALABLE_STOCK_REASONS,
    ).select_related('item')
    if date_from:
        stock_qs = stock_qs.filter(out_date__gte=date_from)
    for s in stock_qs:
        add(s.out_date, 'stock', s)

    # Sales returns. Voided ones are excluded on the same grounds as voided
    # invoices: nothing came back, so there is nothing to reverse. A return
    # posts on the day the goods arrived, not the day of the original sale.
    return_qs = SalesReturn.objects.filter(
        posting_status='unposted', is_voided=False, datetime__date__lte=date_to,
    ).select_related('invoice')
    if date_from:
        return_qs = return_qs.filter(datetime__date__gte=date_from)
    for r in return_qs:
        add(r.datetime.date(), 'sales_return', r)

    return by_date


def _calendar_days(start, end):
    """Every calendar day from ``start`` through ``end`` inclusive."""
    out = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += datetime.timedelta(days=1)
    return out


def _upsert_day_log(day, batch, actor, count):
    """Mark ``day`` as journalled, matching the original view's behaviour.

    Every calendar day in the swept range is marked posted — including days
    with zero documents. Those days are reported to the caller as 'skipped',
    but that is a DISPLAY distinction only; the stored state is identical to
    what the old code wrote.
    """
    log, _created = JournalDayLog.objects.get_or_create(date=day)
    log.is_posted = True
    log.posted_at = timezone.now()
    log.posted_by = actor
    log.batch = batch
    log.transaction_count = (log.transaction_count or 0) + count
    log.save()


def run_journal_sweep(actor, date_to, summary_builder, posters):
    """Post every unposted document dated <= ``date_to``, one day at a time.

    Generator. Yields progress dicts, the last of which is always ``done`` or
    ``error``:

        {'type': 'start', 'batch_id', 'date_to', 'total', 'days': [...]}
        {'type': 'day',   'index', 'date', 'documents', 'status'}
        {'type': 'done',  ...full JournalRunResponse payload...}
        {'type': 'error', 'date', 'message', 'days_committed'}

    ``summary_builder`` and ``posters`` are injected rather than imported to
    keep this module free of a circular dependency on the views package.
    ``posters`` maps kind -> callable(obj).

    Never raises for a posting failure: once a caller has begun streaming, the
    HTTP status is already committed to 200, so failures must travel as an
    'error' event rather than an exception.
    """
    by_date = _gather_events(date_to)
    total_documents = sum(len(v) for v in by_date.values())

    # requested_range mirrors the original view: both ends are date_to until we
    # discover how far back the unposted documents actually reach.
    batch = JournalBatch.objects.create(
        requested_range_start=date_to,
        requested_range_end=date_to,
        status='running',
        run_by=actor,
    )

    if not by_date:
        # Nothing to post. Emit an empty day list, then the same zeroed summary
        # the original endpoint returned.
        yield {
            'type': 'start', 'batch_id': batch.id, 'date_to': date_to.isoformat(),
            'total': 0, 'days': [],
        }
        summary = {
            'total_revenue': '0.00', 'accounts_payable_total': '0.00',
            'spend_by_account': [], 'account_movements': [],
        }
        batch.summary = summary
        batch.status = 'completed'
        batch.save()
        yield _done_payload(batch, 0, summary)
        return

    swept_start = min(by_date)
    days = _calendar_days(swept_start, date_to)

    batch.requested_range_start = swept_start
    batch.swept_range_start = swept_start
    batch.swept_range_end = date_to
    batch.save()

    yield {
        'type': 'start',
        'batch_id': batch.id,
        'date_to': date_to.isoformat(),
        'total': len(days),
        'days': [
            {'date': d.isoformat(), 'documents': len(by_date.get(d, []))}
            for d in days
        ],
    }

    days_committed = 0
    for index, day in enumerate(days):
        events = by_date.get(day, [])
        try:
            # One transaction per day. Documents within the day are atomic
            # together; days are independent of each other.
            with transaction.atomic():
                for kind, obj in events:
                    posters[kind](obj)
                _upsert_day_log(day, batch, actor, len(events))
        except Exception as exc:  # noqa: BLE001 — must be delivered, not raised
            batch.status = 'failed'
            batch.save()
            yield {
                'type': 'error',
                'date': day.isoformat(),
                'message': str(exc) or exc.__class__.__name__,
                'days_committed': days_committed,
            }
            return

        days_committed += 1
        yield {
            'type': 'day',
            'index': index,
            'date': day.isoformat(),
            'documents': len(events),
            'status': 'posted' if events else 'skipped',
        }

    summary = summary_builder(swept_start, date_to)
    batch.summary = summary
    batch.status = 'completed'
    batch.save()
    yield _done_payload(batch, total_documents, summary)


def _done_payload(batch, documents_posted, summary):
    """The exact response shape the non-streaming endpoint has always returned."""
    return {
        'type': 'done',
        'batch_id': batch.id,
        'status': batch.status,
        'requested_range_start': batch.requested_range_start.isoformat(),
        'requested_range_end': batch.requested_range_end.isoformat(),
        'swept_range_start': batch.swept_range_start.isoformat() if batch.swept_range_start else None,
        'swept_range_end': batch.swept_range_end.isoformat() if batch.swept_range_end else None,
        'documents_posted': documents_posted,
        'summary': summary,
    }
