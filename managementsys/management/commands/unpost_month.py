"""Un-post one calendar month so a journal sweep has work to do again.

Built to exercise the live per-day progress animation on the Jalankan Jurnal
page (docs/JOURNAL-RUN-PROGRESS-PLAN.md): it rewinds a month back to
'unposted' so the next sweep re-posts it day by day.

    python manage.py unpost_month --year 2026 --month 6            # dry run
    python manage.py unpost_month --year 2026 --month 6 --apply    # commit

WHAT IT TOUCHES
---------------
Only what a journal sweep can put back. Ledger rows are selected via the
document foreign key, and only where source_type is one of
'invoice'/'purchase'/'transfer'/'expense' — the four the sweep writes.

WHAT IT DELIBERATELY LEAVES ALONE
---------------------------------
* void_memo / edit_memo — written synchronously by the void/edit endpoints and
  dated the day of the void. A sweep NEVER recreates these; deleting them would
  be permanent loss.
* stock / opname / adjustment / manual — inventory movements and hand-written
  journal entries. Not the sweep's output either.
* Voided documents — a voided invoice's original entries are balanced by a
  reversing memo dated later. Deleting the originals while leaving the memo
  would strand the reversal, and the sweep will not repost the document
  (is_voided=False is in its filter). Left fully intact.

BALANCE CACHE
-------------
ChartOfAccounts.balance is a cached running total mutated on every posting, not
derived on read. Deleting ledger rows without recomputing it leaves the number
your financial reports show silently wrong. Every account balance is recomputed
from the surviving ledger at the end, and the trial balance is re-verified.
"""
import calendar
import datetime
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from managementsys.accounting_checks import (
    IMPORTED_INVOICE_PREFIX, balance_drift, trial_balance,
)
from managementsys.models import (
    AccountTransfer, ChartOfAccounts, Expense, Invoice, JournalBatch,
    JournalDayLog, LedgerEntry, PurchaseInvoice,
)

# The only source_types a journal sweep produces. Anything else in the range is
# from another subsystem and must survive.
SWEEP_SOURCE_TYPES = ('invoice', 'purchase', 'transfer', 'expense')

PROTECTED_SOURCE_TYPES = (
    'void_memo', 'edit_memo', 'stock', 'opname', 'adjustment', 'manual',
)


class Command(BaseCommand):
    help = 'Revert one month of journal postings so a sweep can re-post it.'

    def add_arguments(self, parser):
        parser.add_argument('--year', type=int, required=True)
        parser.add_argument('--month', type=int, required=True)
        parser.add_argument(
            '--apply', action='store_true',
            help='Commit. Without this the whole thing runs and rolls back.',
        )
        parser.add_argument(
            '--include-imported', action='store_true',
            help=(
                'Also revert IPOS-* imported invoices. Off by default — see the '
                'warning this command prints about double-counting.'
            ),
        )

    # ── entry point ──────────────────────────────────────────────────────────

    def handle(self, *args, **opts):
        year, month = opts['year'], opts['month']
        if not 1 <= month <= 12:
            raise CommandError('--month must be 1-12.')

        start = datetime.date(year, month, 1)
        end = datetime.date(year, month, calendar.monthrange(year, month)[1])

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'Un-posting {start:%B %Y} ({start} … {end})'
            + ('' if opts['apply'] else '  —  DRY RUN, rolled back at the end')
        ))

        with transaction.atomic():
            docs = self._select_documents(start, end, opts['include_imported'])
            self._report_selection(docs)
            self._warn_about_imported(start, end, opts['include_imported'])
            self._report_protected(start, end)

            deleted = self._delete_ledger_entries(docs)
            flipped = self._flip_documents(docs)
            days = self._clear_day_logs(start, end)
            batches = self._mark_batches(start, end)
            corrected = self._recompute_balances()

            self.stdout.write('')
            self.stdout.write(self.style.MIGRATE_HEADING('Result'))
            self.stdout.write(f'  ledger entries deleted    {deleted}')
            self.stdout.write(f'  documents → unposted      {flipped}')
            self.stdout.write(f'  JournalDayLog rows removed {days}')
            self.stdout.write(f'  JournalBatch rows touched  {batches}')
            self.stdout.write(f'  account balances corrected {corrected}')

            self._verify()

            if not opts['apply']:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING(
                    '\nDRY RUN — everything above was rolled back. '
                    'Re-run with --apply to commit.'
                ))
            else:
                self.stdout.write(self.style.SUCCESS('\nCommitted.'))

    # ── selection ────────────────────────────────────────────────────────────

    def _select_documents(self, start, end, include_imported):
        """Posted, non-voided documents whose transaction date falls in range.

        Voided documents are excluded on purpose: their originals are balanced
        by a later reversing memo, and the sweep will not repost them.
        """
        invoices = Invoice.objects.filter(
            datetime__date__gte=start, datetime__date__lte=end,
            posting_status='posted', is_voided=False,
        )
        if not include_imported:
            invoices = invoices.exclude(
                invoice_number__startswith=f'{IMPORTED_INVOICE_PREFIX}-')

        return {
            'invoice': list(invoices),
            'purchase': list(PurchaseInvoice.objects.filter(
                purchase_date__gte=start, purchase_date__lte=end,
                posting_status='posted', is_voided=False,
            )),
            'transfer': list(AccountTransfer.objects.filter(
                transfer_date__gte=start, transfer_date__lte=end,
                posting_status='posted',
            )),
            'expense': list(Expense.objects.filter(
                expense_date__gte=start, expense_date__lte=end,
                posting_status='posted',
            )),
        }

    def _report_selection(self, docs):
        self.stdout.write('')
        self.stdout.write('Documents to revert:')
        for kind in ('invoice', 'purchase', 'transfer', 'expense'):
            self.stdout.write(f'  {kind:<10} {len(docs[kind])}')
        if not any(docs.values()):
            self.stdout.write(self.style.WARNING(
                '  nothing posted in this range — a sweep would find no work.'))

    def _warn_about_imported(self, start, end, include_imported):
        """IPOS-* invoices are deliberately kept out of the ledger.

        accounting_checks.py: iPos ran alongside CPMS through 2026-06-18 and it
        is not established whether those rows duplicate CPMS-native sales.
        Nothing in the sweep excludes them, and posting_status defaults to
        'unposted' — so a sweep WILL post any that are still unposted. Surfacing
        the count here because a test run is exactly when that would happen
        unnoticed.
        """
        pending = Invoice.objects.filter(
            datetime__date__gte=start, datetime__date__lte=end,
            invoice_number__startswith=f'{IMPORTED_INVOICE_PREFIX}-',
            posting_status='unposted', is_voided=False,
        ).count()

        if not pending:
            return

        self.stdout.write('')
        self.stdout.write(self.style.ERROR(
            f'  !! {pending} imported {IMPORTED_INVOICE_PREFIX}-* invoices in this range '
            f'are already unposted.'
        ))
        self.stdout.write(self.style.WARNING(
            '     They are NOT touched by this command, but the next journal sweep\n'
            '     has no exclusion for them and will post them — which is the\n'
            '     double-count accounting_checks.py warns about. Decide before you\n'
            '     run the sweep.'
        ))

    def _report_protected(self, start, end):
        """Show what is being left behind, so 'why is June not empty' is answered
        before it is asked."""
        tally = defaultdict(int)
        for st in (LedgerEntry.objects
                   .filter(date__gte=start, date__lte=end,
                           source_type__in=PROTECTED_SOURCE_TYPES)
                   .values_list('source_type', flat=True)):
            tally[st] += 1

        if not tally:
            return
        self.stdout.write('')
        self.stdout.write('Ledger rows in range being LEFT ALONE '
                          '(a sweep cannot recreate these):')
        for st in sorted(tally):
            self.stdout.write(f'  {st:<12} {tally[st]}')

    # ── mutation ─────────────────────────────────────────────────────────────

    def _delete_ledger_entries(self, docs):
        """Delete only sweep-authored rows, selected via the document FK."""
        total = 0
        for field, kind in (
            ('invoice', 'invoice'),
            ('purchase_invoice', 'purchase'),
            ('transfer', 'transfer'),
            ('expense', 'expense'),
        ):
            ids = [d.pk for d in docs[kind]]
            if not ids:
                continue
            n, _ = (LedgerEntry.objects
                    .filter(**{f'{field}_id__in': ids},
                            source_type__in=SWEEP_SOURCE_TYPES)
                    .delete())
            total += n
        return total

    def _flip_documents(self, docs):
        total = 0
        for model, kind in (
            (Invoice, 'invoice'),
            (PurchaseInvoice, 'purchase'),
            (AccountTransfer, 'transfer'),
            (Expense, 'expense'),
        ):
            ids = [d.pk for d in docs[kind]]
            if not ids:
                continue
            total += model.objects.filter(pk__in=ids).update(posting_status='unposted')
        return total

    def _clear_day_logs(self, start, end):
        n, _ = JournalDayLog.objects.filter(date__gte=start, date__lte=end).delete()
        return n

    def _mark_batches(self, start, end):
        """Batches whose swept range fell entirely inside the cleared month no
        longer describe reality. Flag rather than delete, so the audit trail of
        'a run happened' survives."""
        return (JournalBatch.objects
                .filter(swept_range_start__gte=start, swept_range_end__lte=end,
                        status='completed')
                .update(status='failed'))

    def _recompute_balances(self):
        """Rebuild every cached ChartOfAccounts.balance from the ledger."""
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
