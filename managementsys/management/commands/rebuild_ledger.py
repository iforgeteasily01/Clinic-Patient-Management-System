"""Tear the general ledger down to nothing and let a sweep build it again.

``unpost_month`` rewinds one calendar month. This is the whole book: every
sweep-authored journal entry from ``--from`` (default: the beginning) onward is
deleted, every document flipped back to 'unposted', every cached account balance
recomputed, and the next journal run re-posts the lot through today's engine.

WHY YOU WOULD DO THIS
---------------------
Because the posting rules changed underneath the data. Revenue that landed in
4200000 "Product Sales Revenue" was posted before per-category routing worked;
lines with no ``item_id`` earned no revenue leg at all until
``_line_revenue_account`` learned to match by name; invoice discounts, tax and
additional charges were not posted at all before migration 0075. Correction
journals can express any single one of those, but not thousands.

WHAT IT WILL NOT REPRODUCE — READ THIS FIRST
--------------------------------------------
**COGS will not come back the same, and that is not a bug you can fix.**
Invoice cost of sales falls out of FIFO consumption against ``InventoryBatch``.
Those batches have been consumed, restocked by voids and edits, topped up by
later purchases and re-consumed many times since the original postings. Re-posting
draws against *today's* batch state, not the state that existed on the day of the
sale. Expect cost of sales — and therefore gross profit — to move.

Also not reproduced:

* **Stock is not re-deducted.** This command never touches ``InventoryBatch``.
  Re-posting recomputes the COGS *number* from current batches but does not
  consume them again, so quantities are safe.
* **Entry numbers change.** ``JE-<year>-NNNNNN`` is allocated from a gapless
  per-year sequence. Rebuilt entries take fresh numbers; anything printed or
  filed against an old number no longer resolves.
* **Correction journals are refused, not rebuilt.** An entry someone corrected
  by hand is a deliberate human statement about the books. See below.

WHAT IT REFUSES TO TOUCH
------------------------
Everything ``unpost_month`` protects, for the same reasons, plus corrections:

* ``void_memo`` / ``edit_memo`` / ``restore_memo`` — written synchronously by
  the void/edit/restore endpoints and dated the day of that action. A sweep
  never recreates them; deleting them would be permanent loss.
* ``stock`` / ``opname`` / ``adjustment`` / ``manual`` — other subsystems and
  hand-written entries. Note that from migration 0105 ``stock`` IS swept, but
  historic stock rows carry no recoverable cost, so they stay out.
* ``reversal`` / ``correction`` — and, transitively, the original entry each one
  points at. ``JournalEntry.reverses``/``corrects`` are PROTECT precisely so a
  delete cannot orphan a correction chain. The command detects these up front
  and **aborts** rather than half-rebuilding around them.
* Voided documents. Their originals are balanced by a later reversing memo;
  deleting the originals would strand the reversal, and the sweep will not
  re-post them (``is_voided=False`` is in its filter).

ORDER OF OPERATIONS
-------------------
Run these in this order, or the rebuild will faithfully re-post the same wrong
numbers it was meant to fix::

    python manage.py migrate                          # 0105 + 0106
    python manage.py void_ipos_duplicate_invoices --apply
    python manage.py rebuild_ledger --from 2026-06-01 --apply
    # then, in the web UI: Akuntansi → Jalankan Jurnal → preview → commit

Dry run by default. ``--apply`` commits. Take a database backup first — the
CPMS-DB-Backup utility exists for exactly this moment.
"""
import datetime
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from managementsys.accounting_checks import (
    IMPORTED_INVOICE_PREFIX, balance_drift, trial_balance,
)
from managementsys.models import (
    AccountTransfer, ChartOfAccounts, Expense, Invoice, JournalBatch,
    JournalDayLog, JournalEntry, LedgerEntry, PurchaseInvoice, StockOutLog,
)

# Exactly what a sweep produces. Anything else in range belongs to another
# subsystem and must survive.
SWEEP_SOURCE_TYPES = ('invoice', 'purchase', 'transfer', 'expense')

PROTECTED_SOURCE_TYPES = (
    'void_memo', 'edit_memo', 'restore_memo', 'stock', 'opname', 'adjustment',
    'manual', 'reversal', 'correction',
)

# (document key, model, date field, extra filters)
DOCUMENT_SPECS = (
    ('invoice',  Invoice,         'datetime__date', {'is_voided': False}),
    ('purchase', PurchaseInvoice, 'purchase_date',  {'is_voided': False}),
    ('transfer', AccountTransfer, 'transfer_date',  {}),
    ('expense',  Expense,         'expense_date',   {}),
)

LEDGER_FK = {
    'invoice': 'invoice',
    'purchase': 'purchase_invoice',
    'transfer': 'transfer',
    'expense': 'expense',
}


class Command(BaseCommand):
    help = 'Delete every sweep-authored journal entry and reset documents so a sweep rebuilds them.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--from', dest='date_from', default=None,
            help='First transaction date to rebuild (YYYY-MM-DD). Omit for everything.',
        )
        parser.add_argument(
            '--to', dest='date_to', default=None,
            help='Last transaction date to rebuild (YYYY-MM-DD). Omit for everything.',
        )
        parser.add_argument(
            '--apply', action='store_true',
            help='Commit. Without it the whole thing runs and rolls back.',
        )
        parser.add_argument(
            '--include-imported', action='store_true',
            help=(
                f'Also reset {IMPORTED_INVOICE_PREFIX}-* imported invoices. Off by '
                'default: they are held out of the ledger on purpose and a sweep '
                'WILL post them once they are unposted. Run '
                'void_ipos_duplicate_invoices first if you turn this on.'
            ),
        )
        parser.add_argument(
            '--force-corrections', action='store_true',
            help=(
                'Proceed even when corrected entries are in range. They are still '
                'not deleted — this only downgrades the abort to a warning.'
            ),
        )

    # ── entry point ──────────────────────────────────────────────────────────

    def handle(self, *args, **opts):
        start, end = self._parse_range(opts)
        w = self.stdout.write

        w(self.style.MIGRATE_HEADING(
            'Rebuilding the ledger '
            + (f'{start or "(beginning)"} … {end or "(today)"}')
            + ('' if opts['apply'] else '  —  DRY RUN, rolled back at the end')
        ))

        with transaction.atomic():
            self._abort_on_corrections(start, end, opts['force_corrections'])

            docs = self._select_documents(start, end, opts['include_imported'])
            self._report_selection(docs)
            self._report_protected(start, end)
            self._warn_about_imported(start, end, opts['include_imported'])

            lines, entries = self._delete_journal(docs)
            flipped = self._flip_documents(docs)
            stock = self._flip_stock_corrections(start, end)
            days = self._clear_day_logs(start, end)
            batches = self._mark_batches(start, end)
            corrected = self._recompute_balances()

            w('')
            w(self.style.MIGRATE_HEADING('Result'))
            w(f'  ledger lines deleted        {lines}')
            w(f'  journal entries deleted     {entries}')
            w(f'  documents → unposted        {flipped}')
            w(f'  stock corrections → unposted {stock}')
            w(f'  JournalDayLog rows removed  {days}')
            w(f'  JournalBatch rows flagged   {batches}')
            w(f'  account balances corrected  {corrected}')

            self._verify()
            self._next_steps(end)

            if not opts['apply']:
                transaction.set_rollback(True)
                w(self.style.WARNING(
                    '\nDRY RUN — everything above was rolled back. '
                    'Re-run with --apply to commit.'))
            else:
                w(self.style.SUCCESS('\nCommitted. The ledger is now empty for this range.'))

    # ── range ────────────────────────────────────────────────────────────────

    def _parse_range(self, opts):
        def parse(raw, label):
            if not raw:
                return None
            try:
                return datetime.date.fromisoformat(raw)
            except ValueError as exc:
                raise CommandError(f'{label} must be YYYY-MM-DD: {exc}') from exc

        start = parse(opts['date_from'], '--from')
        end = parse(opts['date_to'], '--to')
        if start and end and start > end:
            raise CommandError('--from must not be after --to.')
        return start, end

    def _date_filter(self, field, start, end):
        out = {}
        if start:
            out[f'{field}__gte'] = start
        if end:
            out[f'{field}__lte'] = end
        return out

    # ── guards ───────────────────────────────────────────────────────────────

    def _abort_on_corrections(self, start, end, force):
        """Correction chains are human statements about the books.

        ``JournalEntry.reverses``/``corrects`` are PROTECT, so deleting an entry
        somebody corrected raises mid-transaction. Catching it here means the
        operator learns which entries are involved instead of reading a
        ProtectedError traceback.
        """
        qs = JournalEntry.objects.filter(**self._date_filter('date', start, end))
        touched = qs.filter(
            source_type__in=('reversal', 'correction'),
        ) | qs.filter(reversed_by__isnull=False) | qs.filter(corrections__isnull=False)
        touched = touched.distinct()

        count = touched.count()
        if not count:
            return

        self.stdout.write('')
        self.stdout.write(self.style.ERROR(
            f'  {count} journal entries in this range are part of a correction chain:'))
        for entry in touched.order_by('date', 'entry_number')[:20]:
            self.stdout.write(
                f'    {entry.entry_number}  {entry.date}  {entry.source_type}  {entry.memo[:60]}')
        if count > 20:
            self.stdout.write(f'    … and {count - 20} more')

        if not force:
            raise CommandError(
                'Refusing to rebuild a range containing corrections. Rebuilding would\n'
                'discard the original entries those corrections describe and leave the\n'
                'corrections pointing at nothing. Narrow --from/--to to exclude them, or\n'
                'pass --force-corrections to continue (they are still not deleted).'
            )
        self.stdout.write(self.style.WARNING(
            '  --force-corrections given: continuing. These entries and the ones they\n'
            '  reference are NOT deleted, so the rebuilt range will double-count them.'
        ))

    # ── selection ────────────────────────────────────────────────────────────

    def _select_documents(self, start, end, include_imported):
        docs = {}
        for key, model, field, extra in DOCUMENT_SPECS:
            qs = model.objects.filter(
                posting_status='posted', **extra,
                **self._date_filter(field, start, end),
            )
            if key == 'invoice' and not include_imported:
                qs = qs.exclude(
                    invoice_number__startswith=f'{IMPORTED_INVOICE_PREFIX}-')
            docs[key] = list(qs.values_list('pk', flat=True))
        return docs

    def _report_selection(self, docs):
        self.stdout.write('')
        self.stdout.write('Documents to rebuild:')
        for key, _m, _f, _e in DOCUMENT_SPECS:
            self.stdout.write(f'  {key:<10} {len(docs[key])}')
        if not any(docs.values()):
            self.stdout.write(self.style.WARNING(
                '  nothing posted in this range — a sweep would find no work.'))

    def _report_protected(self, start, end):
        tally = defaultdict(int)
        for st in (LedgerEntry.objects
                   .filter(source_type__in=PROTECTED_SOURCE_TYPES,
                           **self._date_filter('date', start, end))
                   .values_list('source_type', flat=True)):
            tally[st] += 1
        if not tally:
            return
        self.stdout.write('')
        self.stdout.write('Ledger rows being LEFT ALONE (a sweep cannot recreate these):')
        for st in sorted(tally):
            self.stdout.write(f'  {st:<12} {tally[st]}')

    def _warn_about_imported(self, start, end, include_imported):
        pending = Invoice.objects.filter(
            invoice_number__startswith=f'{IMPORTED_INVOICE_PREFIX}-',
            posting_status='unposted', is_voided=False,
            **self._date_filter('datetime__date', start, end),
        ).count()
        if not pending:
            return
        self.stdout.write('')
        self.stdout.write(self.style.ERROR(
            f'  !! {pending} un-voided {IMPORTED_INVOICE_PREFIX}-* invoices in this range '
            'are unposted.'))
        self.stdout.write(self.style.WARNING(
            '     Nothing in the sweep excludes them, so the rebuild run WILL post them\n'
            '     alongside their CPMS twins — a straight double-count. Run\n'
            '     `void_ipos_duplicate_invoices --apply` before the sweep.'))

    # ── mutation ─────────────────────────────────────────────────────────────

    def _delete_journal(self, docs):
        """Delete sweep-authored lines, then the headers left with no lines.

        Lines go first and are selected by document FK *and* source_type, so a
        void memo attached to the same invoice survives. Headers are then deleted
        only when nothing references them — which is what keeps a header that
        still owns a protected line alive.
        """
        line_total = 0
        entry_ids = set()
        for key, fk in LEDGER_FK.items():
            ids = docs[key]
            if not ids:
                continue
            qs = LedgerEntry.objects.filter(
                **{f'{fk}__id__in': ids}, source_type__in=SWEEP_SOURCE_TYPES,
            )
            entry_ids.update(
                qs.exclude(journal_entry__isnull=True)
                  .values_list('journal_entry_id', flat=True)
            )
            deleted, _ = qs.delete()
            line_total += deleted

        entry_total = 0
        if entry_ids:
            orphans = JournalEntry.objects.filter(
                pk__in=entry_ids, lines__isnull=True,
                reverses__isnull=True, corrects__isnull=True,
            ).exclude(reversed_by__isnull=False).exclude(corrections__isnull=False)
            entry_total, _ = orphans.delete()
        return line_total, entry_total

    def _flip_documents(self, docs):
        total = 0
        for key, model, _f, _e in DOCUMENT_SPECS:
            ids = docs[key]
            if not ids:
                continue
            total += model.objects.filter(pk__in=ids).update(posting_status='unposted')
        return total

    def _flip_stock_corrections(self, start, end):
        """Stock corrections written since migration 0105 can be re-posted.

        Only rows that actually carry a cost: the backfilled historic rows have
        ``value=0`` and were stamped 'posted' precisely so a sweep would ignore
        them, and flipping those back would achieve nothing but noise.
        """
        return StockOutLog.objects.filter(
            posting_status='posted', value__gt=0,
            **self._date_filter('out_date', start, end),
        ).update(posting_status='unposted')

    def _clear_day_logs(self, start, end):
        deleted, _ = JournalDayLog.objects.filter(
            **self._date_filter('date', start, end)).delete()
        return deleted

    def _mark_batches(self, start, end):
        """Flag rather than delete: 'a run happened' is audit history even once
        its output is gone."""
        flt = {'status': 'completed'}
        if start:
            flt['swept_range_start__gte'] = start
        if end:
            flt['swept_range_end__lte'] = end
        return JournalBatch.objects.filter(**flt).update(status='failed')

    def _recompute_balances(self):
        """``ChartOfAccounts.balance`` is a cached running total mutated on every
        posting, not derived on read. Deleting ledger rows without rebuilding it
        leaves every financial report silently wrong."""
        corrected = 0
        for account, _stored, derived in balance_drift():
            ChartOfAccounts.objects.filter(pk=account.pk).update(balance=derived)
            corrected += 1
        return corrected

    # ── verification ─────────────────────────────────────────────────────────

    def _verify(self):
        dr, cr = trial_balance()
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('Verification'))
        self.stdout.write(f'  total debits   {dr:,.2f}')
        self.stdout.write(f'  total credits  {cr:,.2f}')
        if dr == cr:
            self.stdout.write(self.style.SUCCESS('  trial balance BALANCED'))
        else:
            self.stdout.write(self.style.ERROR(
                f'  trial balance OUT BY {abs(dr - cr):,.2f} — do not --apply'))

        remaining = balance_drift()
        if remaining:
            self.stdout.write(self.style.ERROR(
                f'  {len(remaining)} account balances still drifting'))
        else:
            self.stdout.write(self.style.SUCCESS(
                '  all cached account balances match the ledger'))

    def _next_steps(self, end):
        target = (end or datetime.date.today()).isoformat()
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING('Next'))
        self.stdout.write(
            '  The ledger is empty for this range — reports will read zero until you\n'
            '  re-post. In the web UI: Akuntansi → Jalankan Jurnal, preview to\n'
            f'  date_to={target}, review, then commit.\n'
            '\n'
            '  Expect cost of sales to differ from the original postings. FIFO is\n'
            '  recomputed against current batches, not the batches that existed on\n'
            '  the day of each sale. Reconcile gross profit, not just revenue.'
        )
