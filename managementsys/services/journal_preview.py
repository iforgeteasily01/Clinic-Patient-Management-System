"""Two-phase journal run: preview into staging, then commit to the ledger.

WHY THIS EXISTS
===============
``journal_sweep.run_journal_sweep`` computes and writes in one pass — by the
time the operator sees a result the ledger has already moved. This module
splits that in half:

    preview(date_to)  → JournalStagingBatch + StagedJournalEntry/Line rows
                        (nothing in the ledger, no document touched)
    commit(batch)     → JournalEntry + LedgerEntry rows, posting_status flips,
                        JournalDayLog upserts, JournalBatch summary

Both phases reuse ``journal_sweep._gather_events`` so the two can never disagree
about *which* documents are in scope, and both emit the same SSE event shape the
web UI already animates.

WHAT COMMIT ACTUALLY POSTS — READ THIS
======================================
Commit does **not** blindly replay the staged lines. It rebuilds each entry from
its source document using the same ``build_*_legs`` function the preview called,
and posts that.

That sounds like it defeats the point of staging, but it is the stronger
guarantee, for two reasons:

  1. Every staged entry carries a ``source_fingerprint``. Commit re-verifies all
     of them up front and refuses the whole batch if any document changed since
     the review. So for deterministic documents — purchases, expenses, transfers
     — "rebuild" and "replay" provably produce the same rows.
  2. Invoice COGS is *not* deterministic. It falls out of FIFO consumption, and
     stock moves. Those lines have to be recomputed against real batches at
     commit time or the posting would be wrong. Rebuilding is the only way to
     get that right, and doing it uniformly means one code path rather than a
     replay path plus a recompute path that could drift.

Where a rebuilt amount differs from what was staged, the difference is recorded
on the staging batch as a variance note and reported back to the operator. Those
lines were flagged ``is_estimated`` in the preview, so the difference is never a
surprise.

TRANSACTION BOUNDARIES
======================
Preview is disposable, so it runs in a single transaction and rolls back whole.

Commit keeps the sweep's semantics exactly: **one transaction per calendar day**.
A failure on day *k* leaves days 1…*k*-1 posted and not rolled back, reports
``days_committed``, and a re-run resumes from *k*. Do not wrap the commit loop in
an outer atomic block — that would silently restore all-or-nothing behaviour.
"""

import datetime
import hashlib
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from ..models import (
    AccountTransfer, ChartOfAccounts, Expense, Invoice, JournalBatch,
    JournalDayLog, JournalStagingBatch, PurchaseInvoice, SalesReturn,
    StagedJournalEntry, StagedJournalLine, StockOutLog,
)
from .journal_engine import (
    build_expense_legs, build_purchase_legs, build_stock_correction_legs,
    build_transfer_legs, reserve_entry_numbers, write_legs,
)
from .journal_sweep import _calendar_days, _gather_events

# How long an unreviewed draft survives. Long enough to walk away from, short
# enough that nobody commits last week's numbers by accident.
DRAFT_TTL = datetime.timedelta(hours=24)

# Staging rows of a committed batch are kept this long as an audit aid.
COMMITTED_RETENTION = datetime.timedelta(days=7)

CENT = Decimal('0.01')


# ── Fingerprints ──────────────────────────────────────────────────────────────

def _fingerprint(parts) -> str:
    joined = '|'.join('' if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode('utf-8')).hexdigest()


def invoice_gl_parts(obj, *, splits=None) -> list:
    """Everything an invoice's journal *legs* are derived from.

    Deliberately excludes ``datetime``: it selects which day the sweep posts the
    invoice on, but changes no leg. ``fingerprint_document`` adds it back,
    because a preview staged for one day must go stale if the invoice moves to
    another. The edit path (``InvoiceDetailView.put``) wants the narrower set —
    see ``invoice_gl_fingerprint``.

    Anything the posting reads must appear here. An omission means an edit that
    changes the GL is mistaken for a no-op; a spurious inclusion only costs a
    redundant reversal/repost pair, so err on the side of including.

    ``splits`` overrides the persisted InvoicePayment rows. The edit path needs
    that: it must fingerprint the invoice's *intended* final state while the old
    split rows are still in the table, because the reversal that runs in between
    rebuilds the old legs from them.
    """
    lines = list(obj.items.values_list('item_id', 'item_name', 'quantity', 'price'))
    # Split payments are part of the entry's debit side — editing one changes
    # which accounts get debited without touching any scalar below.
    if splits is None:
        splits = list(obj.payments.values_list(
            'payment_account_id', 'payment_method_id', 'amount', 'sort_order'))
    return [
        'invoice', obj.pk, obj.grand_total, obj.tax,
        obj.additional_charges, obj.discount, obj.payment_method_id,
        # obj.payment_account_id resolves the payment leg's GL account
        # (see journal_engine._revenue_legs) — without it here, changing
        # an invoice's bank account would not invalidate a staged preview
        # entry, and commit would post the stale (wrong) account.
        obj.payment_account_id,
        obj.warehouse_id, obj.is_voided, lines, splits,
    ]


def invoice_gl_fingerprint(obj, *, splits=None) -> str:
    """Hash of ``invoice_gl_parts`` — equal before and after an edit means the
    edit cannot have changed a single ledger leg."""
    return _fingerprint(invoice_gl_parts(obj, splits=splits))


def fingerprint_document(kind, obj) -> str:
    """A stable hash of everything the document's journal entry is derived from.

    None of these models carry an ``updated_at``, so the fingerprint covers the
    value fields directly — which is in fact what we want: it changes exactly
    when the posting would change, and not when an unrelated column is touched.
    """
    if kind == 'invoice':
        parts = invoice_gl_parts(obj)
        # datetime sits at index 2 to keep this hash byte-identical to the
        # pre-split version, so drafts staged before this change stay valid.
        parts.insert(2, obj.datetime)
        return _fingerprint(parts)
    if kind == 'purchase':
        lines = list(obj.items.values_list(
            'line_type', 'item_id', 'item_name', 'quantity',
            'unit_cost', 'total_discount', 'expense_account_id', 'warehouse_id',
        ))
        return _fingerprint([
            'purchase', obj.pk, obj.purchase_date, obj.total_amount,
            obj.supplier_id, obj.is_voided, lines,
        ])
    if kind == 'expense':
        lines = list(obj.items.values_list('account_id', 'amount', 'description'))
        return _fingerprint([
            'expense', obj.pk, obj.expense_date, obj.total_amount, obj.amount_paid,
            obj.payment_account_id, obj.payment_method_id, obj.payment_memo, lines,
        ])
    if kind == 'transfer':
        return _fingerprint([
            'transfer', obj.pk, obj.transfer_date, obj.amount,
            obj.from_account_id, obj.to_account_id, obj.description,
        ])
    if kind == 'stock':
        # ``value`` is the whole amount and ``reason`` picks the account, so a
        # change to either must invalidate a staged entry. ``quantity`` rides
        # along because it is what an operator would edit first.
        return _fingerprint([
            'stock', obj.pk, obj.out_date, obj.reason, obj.value,
            obj.quantity, obj.item_id, obj.warehouse_id, obj.notes,
        ])
    if kind == 'sales_return':
        # The parts list lives with the return logic rather than here, so the
        # hash cannot drift from what the builder actually reads.
        from .sales_returns import fingerprint as sales_return_fingerprint
        return _fingerprint(sales_return_fingerprint(obj))
    return _fingerprint([kind, obj.pk])


# ── Leg building per document kind ────────────────────────────────────────────

def _invoice_lines(invoice):
    """Rebuild the (line dicts, items_by_id) pair the invoice builder expects.

    Same shape ``accounting_page._post_invoice_for_run`` assembles — kept here so
    preview and commit read the invoice identically.
    """
    items = list(
        invoice.items
        .select_related('item__item_category__revenue_account')
        .all()
    )
    items_by_id = {it.item_id: it.item for it in items if it.item_id}
    line_dicts = [
        {'item_id': it.item_id, 'item_name': it.item_name,
         'quantity': it.quantity, 'price': it.price}
        for it in items
    ]
    return line_dicts, items_by_id


def _purchase_parsed(pinv):
    """Rebuild the ``(parsed, item_objs)`` pair the purchase builder expects."""
    items = list(pinv.items.select_related('item', 'expense_account', 'warehouse').all())
    parsed = [{
        'line_type':       it.line_type,
        'item_id':         it.item_id,
        'item_name':       it.item_name,
        'quantity':        it.quantity,
        'unit':            it.unit,
        'unit_cost':       it.unit_cost,
        'total_discount':  it.total_discount,
        'adjusted_sub':    it.quantity * it.actual_unit_cost,
        'expense_acct_id': it.expense_account_id,
        'warehouse_id':    it.warehouse_id,
    } for it in items]
    return parsed, items


def build_legs_for(kind, obj, *, deduct, sim=None):
    """The LegSet for one document.

    ``deduct=False`` is the preview: no stock consumed, no COA rows created,
    FIFO answered from ``sim``. ``deduct=True`` is the commit: real FIFO, real
    account provisioning.
    """
    # Local import: invoice_page imports journal_engine, so importing it at
    # module scope here would close a cycle at app load.
    from ..views.invoice_page import build_invoice_legs

    if kind == 'invoice':
        line_dicts, items_by_id = _invoice_lines(obj)
        return build_invoice_legs(obj, line_dicts, items_by_id, deduct=deduct, sim=sim)
    if kind == 'purchase':
        parsed, item_objs = _purchase_parsed(obj)
        return build_purchase_legs(obj, parsed, item_objs, obj.supplier, obj.total_amount,
                                   create_accounts=deduct)
    if kind == 'expense':
        items = list(obj.items.select_related('account').all())
        return build_expense_legs(obj, items, create_accounts=deduct)
    if kind == 'transfer':
        return build_transfer_legs(obj)
    if kind == 'stock':
        # Not FIFO-dependent: the cost was captured when the stock was deducted,
        # so preview and commit read the same stored number and the legs are
        # never flagged as estimates.
        return build_stock_correction_legs(obj, create_accounts=deduct)
    if kind == 'sales_return':
        # FIFO-dependent in the same way the invoice is, but in the other
        # direction: the preview estimates what restocking would cost back and
        # flags those legs, the commit does the restock and posts the real
        # figure. See services/sales_returns.build_sales_return_legs.
        from .sales_returns import build_sales_return_legs
        return build_sales_return_legs(obj, deduct=deduct, sim=sim)
    raise ValueError(f'Unknown document kind: {kind}')


SOURCE_TYPE_BY_KIND = {
    'invoice':      'invoice',
    'purchase':     'purchase',
    'transfer':     'transfer',
    'expense':      'expense',
    'stock':        'stock',
    'sales_return': 'sales_return',
}

MODEL_BY_KIND = {
    'invoice':      Invoice,
    'purchase':     PurchaseInvoice,
    'transfer':     AccountTransfer,
    'expense':      Expense,
    'stock':        StockOutLog,
    'sales_return': SalesReturn,
}


def document_label(kind, obj):
    if kind == 'invoice':
        return obj.invoice_number or f'Invoice #{obj.pk}'
    if kind == 'purchase':
        return obj.internal_id or f'Pembelian #{obj.pk}'
    if kind == 'expense':
        return f'Beban #{obj.pk}'
    if kind == 'stock':
        return f'Koreksi stok #{obj.pk}'
    if kind == 'sales_return':
        return obj.return_number or f'Retur #{obj.pk}'
    return f'Transfer #{obj.pk}'


# ── Draft lifecycle ───────────────────────────────────────────────────────────

def purge_expired_drafts():
    """Delete drafts nobody committed and staging rows of long-committed batches.

    Called at the start of every preview so the tables stay small without
    depending on a scheduled task existing; ``purge_journal_drafts`` runs the
    same thing from cron for installs that leave the app idle.
    """
    now = timezone.now()
    JournalStagingBatch.objects.filter(
        status__in=['draft', 'failed'], expires_at__lt=now,
    ).delete()
    JournalStagingBatch.objects.filter(
        status__in=['committed', 'discarded'],
        created_at__lt=now - COMMITTED_RETENTION,
    ).delete()


def open_draft():
    """The single shared draft, or None. Accounting is one set of books, so
    there is deliberately never more than one."""
    return (
        JournalStagingBatch.objects
        .filter(status='draft', expires_at__gt=timezone.now())
        .order_by('-created_at')
        .first()
    )


def discard_open_drafts():
    JournalStagingBatch.objects.filter(status='draft').update(
        status='discarded', error_message='',
    )


# ── Phase 1: preview ──────────────────────────────────────────────────────────

def build_preview(actor, date_to, date_from=None):
    """Materialise everything a sweep to ``date_to`` would post, into staging.

    ``date_from``, when given, is a hard lower bound: a document dated before
    it is left un-journaled even if unposted. Without it the sweep reaches
    back as far as there are stragglers.

    Generator, same event vocabulary as ``run_journal_sweep``:

        {'type': 'start', 'staging_batch_id', 'date_to', 'total', 'days': [...]}
        {'type': 'day',   'index', 'date', 'documents', 'status'}
        {'type': 'done',  ...preview summary...}
        {'type': 'error', 'date', 'message'}

    Unlike the commit, a failed preview is worthless rather than dangerous, so
    this runs in one transaction and rolls back whole.
    """
    # Local import keeps the FIFO simulation out of this module's import graph
    # until it is actually needed.
    from ..views.inventory_page import FifoSimulation

    purge_expired_drafts()
    discard_open_drafts()

    by_date = _gather_events(date_to, date_from)
    total_documents = sum(len(v) for v in by_date.values())

    batch = JournalStagingBatch.objects.create(
        date_to=date_to,
        date_from=date_from,
        status='draft',
        created_by=actor,
        expires_at=timezone.now() + DRAFT_TTL,
    )

    if not by_date:
        yield {'type': 'start', 'staging_batch_id': batch.id,
               'date_to': date_to.isoformat(), 'total': 0, 'days': []}
        yield _preview_done(batch)
        return

    swept_start = min(by_date)
    days = _calendar_days(swept_start, date_to)

    yield {
        'type': 'start',
        'staging_batch_id': batch.id,
        'date_to': date_to.isoformat(),
        'total': len(days),
        'days': [{'date': d.isoformat(), 'documents': len(by_date.get(d, []))} for d in days],
    }

    # One simulation for the whole preview: two invoices drawing on the same
    # batch must not both be told the stock is there.
    sim = FifoSimulation()
    totals = {'debit': Decimal('0'), 'credit': Decimal('0'), 'entries': 0}

    try:
        with transaction.atomic():
            for index, day in enumerate(days):
                events = by_date.get(day, [])
                for sequence, (kind, obj) in enumerate(events):
                    _stage_entry(batch, day, sequence, kind, obj, sim, totals)
                yield {
                    'type': 'day', 'index': index, 'date': day.isoformat(),
                    'documents': len(events),
                    'status': 'staged' if events else 'skipped',
                }

            batch.entry_count = totals['entries']
            batch.document_count = total_documents
            batch.total_debit = totals['debit']
            batch.total_credit = totals['credit']
            batch.save(update_fields=['entry_count', 'document_count',
                                      'total_debit', 'total_credit'])
    except Exception as exc:  # noqa: BLE001 — must travel as an event, not a 500
        JournalStagingBatch.objects.filter(pk=batch.pk).update(
            status='failed', error_message=str(exc) or exc.__class__.__name__,
        )
        yield {'type': 'error', 'date': None,
               'message': str(exc) or exc.__class__.__name__}
        return

    batch.refresh_from_db()
    yield _preview_done(batch)


def _stage_entry(batch, day, sequence, kind, obj, sim, totals):
    """Compute one document's legs and persist them as a staged entry."""
    legset = build_legs_for(kind, obj, deduct=False, sim=sim)
    if not legset.legs:
        return

    debit = legset.total_debit
    credit = legset.total_credit
    has_estimate = any(l.is_estimated for l in legset.legs)

    warnings = list(legset.warnings)
    if debit != credit:
        warnings.append(f'Tidak seimbang: debit {debit} vs kredit {credit}.')
    for leg in legset.legs:
        if leg.account is None and leg.pending_account_label:
            warnings.append(f'Akun "{leg.pending_account_label}" akan dibuat saat pencatatan.')

    entry = StagedJournalEntry.objects.create(
        batch=batch,
        date=day,
        sequence=sequence,
        source_type=SOURCE_TYPE_BY_KIND[kind],
        source_model=kind,
        source_id=obj.pk,
        source_label=document_label(kind, obj)[:120],
        memo=legset.memo[:255],
        total_debit=debit,
        total_credit=credit,
        is_balanced=(debit == credit),
        has_estimate=has_estimate,
        warnings=warnings,
        source_fingerprint=fingerprint_document(kind, obj),
    )

    StagedJournalLine.objects.bulk_create([
        StagedJournalLine(
            entry=entry,
            account=leg.account,
            pending_account_label=leg.pending_account_label[:120],
            entry_type=leg.entry_type,
            amount=leg.amount,
            description=leg.description[:255],
            is_estimated=leg.is_estimated,
            sequence=i,
        )
        for i, leg in enumerate(legset.legs)
    ], batch_size=500)

    totals['debit'] += debit
    totals['credit'] += credit
    totals['entries'] += 1


def _preview_done(batch):
    return {
        'type': 'done',
        'staging_batch_id': batch.id,
        'status': batch.status,
        'date_to': batch.date_to.isoformat(),
        'entry_count': batch.entry_count,
        'document_count': batch.document_count,
        'total_debit': str(batch.total_debit),
        'total_credit': str(batch.total_credit),
        'expires_at': batch.expires_at.isoformat(),
    }


# ── Phase 2: commit ───────────────────────────────────────────────────────────

class StaleDraftError(Exception):
    """A source document changed after the operator reviewed the draft."""

    def __init__(self, stale):
        self.stale = stale
        super().__init__('Dokumen sumber berubah setelah pratinjau dibuat.')


def check_freshness(batch):
    """Every staged entry whose source document no longer matches its fingerprint.

    Returns a list of ``{staged_entry_id, source_label, reason}``. Run before a
    single row is posted — a commit that starts and then discovers staleness on
    day nine has already written eight days of numbers nobody approved.
    """
    stale = []
    by_kind = {}
    for entry in batch.entries.all().only('id', 'source_model', 'source_id',
                                          'source_label', 'source_fingerprint'):
        by_kind.setdefault(entry.source_model, []).append(entry)

    for kind, entries in by_kind.items():
        model = MODEL_BY_KIND[kind]
        qs = model.objects.filter(pk__in=[e.source_id for e in entries])
        if kind != 'transfer':      # transfers have no line rows to prefetch
            qs = qs.prefetch_related('items')
        objs = {o.pk: o for o in qs}
        for entry in entries:
            obj = objs.get(entry.source_id)
            if obj is None:
                stale.append({'staged_entry_id': entry.id,
                              'source_label': entry.source_label,
                              'reason': 'Dokumen sudah dihapus.'})
                continue
            if fingerprint_document(kind, obj) != entry.source_fingerprint:
                stale.append({'staged_entry_id': entry.id,
                              'source_label': entry.source_label,
                              'reason': 'Dokumen berubah setelah pratinjau.'})
    return stale


def commit_preview(actor, batch, summary_builder):
    """Turn a reviewed draft into real journal entries.

    Generator, same events as the preview plus a ``done`` payload shaped like
    the one ``JournalRunView`` has always returned, so the existing summary UI
    keeps working.

    One transaction per calendar day. See the module docstring.
    """
    # Claim the draft before doing anything else. ``JournalPreviewCommitView``
    # also checks the status, but it does so while building the response — the
    # generator does not start until the stream is consumed, so two commits
    # arriving together (a double-clicked button, a retried request) both pass
    # that check and both post. A conditional UPDATE is the only check that is
    # atomic with the transition.
    if not JournalStagingBatch.objects.filter(pk=batch.pk, status='draft').update(
            status='committing'):
        current = JournalStagingBatch.objects.filter(pk=batch.pk).values_list(
            'status', flat=True).first()
        yield {'type': 'error', 'date': None,
               'message': f'Draf ini berstatus "{current or "hilang"}" dan tidak bisa dicatat.',
               'days_committed': 0}
        return

    stale = check_freshness(batch)
    if stale:
        JournalStagingBatch.objects.filter(pk=batch.pk).update(status='draft')
        yield {'type': 'stale', 'stale': stale,
               'message': 'Dokumen sumber berubah setelah pratinjau dibuat. '
                          'Jalankan ulang pratinjau.'}
        return

    days = sorted(set(batch.entries.values_list('date', flat=True)))
    if not days:
        # An empty draft still produces a batch, so the run shows up in history.
        journal_batch = JournalBatch.objects.create(
            requested_range_start=batch.date_to, requested_range_end=batch.date_to,
            status='completed', run_by=actor,
            summary={'total_revenue': '0.00', 'accounts_payable_total': '0.00',
                     'spend_by_account': [], 'account_movements': []},
        )
        JournalStagingBatch.objects.filter(pk=batch.pk).update(
            status='committed', committed_batch=journal_batch, days_committed=0,
        )
        yield {'type': 'start', 'batch_id': journal_batch.id,
               'date_to': batch.date_to.isoformat(), 'total': 0, 'days': []}
        yield _commit_done(journal_batch, 0, journal_batch.summary, [])
        return

    swept_start = min(days)
    calendar = _calendar_days(swept_start, batch.date_to)

    journal_batch = JournalBatch.objects.create(
        requested_range_start=swept_start,
        requested_range_end=batch.date_to,
        swept_range_start=swept_start,
        swept_range_end=batch.date_to,
        status='running',
        run_by=actor,
    )
    JournalStagingBatch.objects.filter(pk=batch.pk).update(
        committed_batch=journal_batch,
    )

    entries_by_date = {}
    for entry in batch.entries.all():
        entries_by_date.setdefault(entry.date, []).append(entry)

    yield {
        'type': 'start',
        'batch_id': journal_batch.id,
        'staging_batch_id': batch.id,
        'date_to': batch.date_to.isoformat(),
        'total': len(calendar),
        'days': [{'date': d.isoformat(), 'documents': len(entries_by_date.get(d, []))}
                 for d in calendar],
    }

    days_committed = 0
    documents_posted = 0
    variances = []
    skipped = []

    for index, day in enumerate(calendar):
        staged = entries_by_date.get(day, [])
        posted_today = 0
        try:
            # One transaction per day. Documents within the day are atomic
            # together; days are independent of each other. Do not hoist this.
            with transaction.atomic():
                numbers = reserve_entry_numbers(day.year, len(staged))
                for staged_entry, number in zip(staged, numbers):
                    try:
                        variances.extend(
                            _commit_entry(staged_entry, number, journal_batch, actor)
                        )
                    except AlreadyPostedError:
                        # Its reserved number is simply burned. Gaps in the
                        # sequence are cheap; posting the document twice is not.
                        skipped.append({
                            'staged_entry_id': staged_entry.id,
                            'source_label': staged_entry.source_label,
                            'date': staged_entry.date.isoformat(),
                            'reason': 'Dokumen sudah dicatat di jurnal lain.',
                        })
                        continue
                    posted_today += 1
                _upsert_day_log(day, journal_batch, actor, posted_today)
        except Exception as exc:  # noqa: BLE001 — delivered as an event
            journal_batch.status = 'failed'
            journal_batch.save(update_fields=['status'])
            JournalStagingBatch.objects.filter(pk=batch.pk).update(
                status='failed', days_committed=days_committed,
                error_message=str(exc) or exc.__class__.__name__,
            )
            yield {'type': 'error', 'date': day.isoformat(),
                   'message': str(exc) or exc.__class__.__name__,
                   'days_committed': days_committed}
            return

        days_committed += 1
        documents_posted += posted_today
        yield {'type': 'day', 'index': index, 'date': day.isoformat(),
               'documents': posted_today,
               'status': 'posted' if posted_today else 'skipped'}

    summary = summary_builder(swept_start, batch.date_to)
    journal_batch.summary = summary
    journal_batch.status = 'completed'
    journal_batch.save(update_fields=['summary', 'status'])
    JournalStagingBatch.objects.filter(pk=batch.pk).update(
        status='committed', days_committed=days_committed, variance_notes=variances,
    )
    yield _commit_done(journal_batch, documents_posted, summary, variances, skipped)


class AlreadyPostedError(Exception):
    """The document was posted by something else between preview and commit.

    Not an error the operator caused, and not fatal: the entry is skipped and
    the rest of the day still posts.
    """


def _commit_entry(staged_entry, entry_number, journal_batch, actor):
    """Post one staged entry for real. Returns any variance notes.

    Raises ``AlreadyPostedError`` when the document has already been journalled,
    rather than posting it a second time.
    """
    kind = staged_entry.source_model
    model = MODEL_BY_KIND[kind]

    # The last line of defence against a double posting, and the only one that
    # holds under concurrency: the row is locked for the rest of this day's
    # transaction, so a second commit racing this one blocks here and then sees
    # 'posted'. Everything upstream — the sweep's posting_status filter, the
    # draft claim, the view's status check — narrows the window; this closes it.
    # A duplicate here is not cosmetic: it double-counts revenue and consumes
    # stock twice.
    obj = model.objects.select_for_update().get(pk=staged_entry.source_id)
    if obj.posting_status == 'posted':
        raise AlreadyPostedError(staged_entry.source_label)

    # Rebuild rather than replay — see the module docstring. The fingerprint
    # check already proved the source is unchanged, so for deterministic
    # documents this is identical to the staged lines; for invoices it is the
    # only way to get FIFO right.
    legset = build_legs_for(kind, obj, deduct=True)

    entry = write_legs(
        legset,
        date=staged_entry.date,
        source_type=staged_entry.source_type,
        document=obj,
        batch=journal_batch,
        actor=actor,
        memo=staged_entry.memo,
        entry_number=entry_number,
    )

    obj.posting_status = 'posted'
    obj.save(update_fields=['posting_status'])

    return _variance_notes(staged_entry, legset, entry)


def _variance_notes(staged_entry, legset, entry):
    """Where the posted amounts differ from what the operator reviewed.

    Only estimated (FIFO-derived) lines can legitimately differ, so anything
    else showing up here is worth reporting loudly rather than swallowing.
    """
    posted_debit = legset.total_debit
    posted_credit = legset.total_credit
    if posted_debit == staged_entry.total_debit and posted_credit == staged_entry.total_credit:
        return []
    return [{
        'entry_number': entry.entry_number if entry else None,
        'staged_entry_id': staged_entry.id,
        'source_label': staged_entry.source_label,
        'date': staged_entry.date.isoformat(),
        'estimated': staged_entry.has_estimate,
        'preview_debit': str(staged_entry.total_debit),
        'posted_debit': str(posted_debit),
        'preview_credit': str(staged_entry.total_credit),
        'posted_credit': str(posted_credit),
    }]


def _upsert_day_log(day, batch, actor, count):
    """Mark ``day`` journalled. Identical to the sweep's version — every calendar
    day in the range is marked posted, including days with zero documents."""
    log, _created = JournalDayLog.objects.get_or_create(date=day)
    log.is_posted = True
    log.posted_at = timezone.now()
    log.posted_by = actor
    log.batch = batch
    log.transaction_count = (log.transaction_count or 0) + count
    log.save()


def run_and_commit(actor, date_to, summary_builder):
    """Preview and immediately commit, yielding only the commit's events.

    This is what the legacy one-shot endpoints (``/journal/run/`` and
    ``/journal/run/stream/``) became. They are kept for scripts and the POS,
    but they must not have their own posting implementation: a second path that
    wrote LedgerEntry rows without a JournalEntry header would quietly reopen
    the flat-ledger problem this whole phase exists to close.

    The preview half is drained silently — the caller asked for a run, not a
    review, so only the commit's per-day events are worth reporting.

    Note this discards any open draft, exactly as a manual preview would. That
    is the right outcome: the documents in that draft are about to be posted by
    this run, which would have made the draft stale anyway.
    """
    batch_id = None
    for evt in build_preview(actor, date_to):
        if evt['type'] == 'error':
            yield {'type': 'error', 'date': evt.get('date'),
                   'message': evt['message'], 'days_committed': 0}
            return
        if evt['type'] == 'done':
            batch_id = evt['staging_batch_id']

    if batch_id is None:
        yield {'type': 'error', 'date': None,
               'message': 'Pratinjau jurnal gagal.', 'days_committed': 0}
        return

    batch = JournalStagingBatch.objects.get(pk=batch_id)
    yield from commit_preview(actor, batch, summary_builder)


def _commit_done(batch, documents_posted, summary, variances, skipped=()):
    """The payload shape ``JournalRunView`` has always returned, plus variances
    and any entries skipped because their document was already posted."""
    return {
        'skipped': list(skipped),
        'type': 'done',
        'batch_id': batch.id,
        'status': batch.status,
        'requested_range_start': batch.requested_range_start.isoformat(),
        'requested_range_end': batch.requested_range_end.isoformat(),
        'swept_range_start': batch.swept_range_start.isoformat() if batch.swept_range_start else None,
        'swept_range_end': batch.swept_range_end.isoformat() if batch.swept_range_end else None,
        'documents_posted': documents_posted,
        'summary': summary,
        'variances': variances,
    }
