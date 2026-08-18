"""Void iPos-imported invoices that duplicate a CPMS-native invoice.

THE SITUATION
-------------
iPos and CPMS ran in parallel from 2026-06-02 to 2026-06-18. Both systems have
rows in this database: CPMS-native sales as ``INV-*`` and the iPos history sync
as ``IPOS-*``. Some visits were keyed into both, so the same money is counted
twice by every report that reads Invoice directly.

Measured against the 2026-07-31 snapshot, in the overlap window:

    CPMS-native    366 invoices   Rp 223.382.700
    iPos-imported  526 invoices   Rp 256.722.863
    exact twins    ~150 invoices  Rp  86.622.200

READ THIS BEFORE --apply
------------------------
**Most iPos invoices in the window are not duplicates.** Only about 150 of 526
have a CPMS twin; the other ~376 are real sales that were never keyed into CPMS
at all, because the CPMS side was a pilot running alongside the real till. This
command voids the twins and nothing else. Voiding the whole ``IPOS-*`` range
would delete roughly Rp 170 juta of genuine revenue and drop June below the
accountant's figure rather than reconciling to it.

Deduplicating the exact twins leaves June around Rp 539 jt against the
accountant's Rp 413,7 jt. That residual is NOT this command's to fix: it is
partial re-keying (one visit split across two CPMS invoices, one of which also
exists in iPos) and it needs a human. Those cases are reported, never voided.

MATCHING
--------
Deliberately strict, because voiding a real sale is worse than leaving a
duplicate standing:

* Both invoices must fall in the parallel-run window and be un-voided.
* Same calendar date, same ``grand_total``, and an identical set of
  ``(resolved item name, quantity, price)`` line signatures.
* Matching is 1:1 and greedy by invoice number — a CPMS invoice can absorb at
  most one iPos twin, so two iPos rows against a single CPMS sale leave the
  second standing for review.
* Invoices with no lines are skipped outright.

Patient number is compared but not required: 19 of the ~150 twins carry
different ``patient_no`` values because the iPos sync created its own patient
rows. A mismatch is reported alongside the decision rather than blocking it,
since date + total + full line signature is already a far stronger key.

JOURNAL
-------
``accounting_checks.IMPORTED_INVOICE_PREFIX`` keeps ``IPOS-*`` invoices out of
the ledger on purpose, so in the normal case a twin has no ledger rows and there
is nothing to reverse. If a sweep has posted one anyway, this command writes a
proper reversing journal entry dated today (``source_type='void_memo'``) rather
than deleting history — the same treatment the void endpoint gives a posted
invoice.

Read-only by default; pass --apply to commit.

    python manage.py void_ipos_duplicate_invoices
    python manage.py void_ipos_duplicate_invoices --apply
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from managementsys.accounting_checks import IMPORTED_INVOICE_PREFIX
from managementsys.models import AuditLog, Invoice, JournalEntry
from managementsys.services.journal_engine import (
    legset_from_entry, reverse_legset, write_legs,
)
from managementsys.views.crm_page import refresh_crm_profile

D = Decimal

# The parallel run. Both ends inclusive. iPos wrote nothing after the 18th and
# CPMS has been the only till since the 19th, so nothing outside this window can
# be a cross-system duplicate.
PARALLEL_START = date(2026, 6, 2)
PARALLEL_END = date(2026, 6, 18)


def line_signature(invoice):
    """A hashable, order-independent fingerprint of an invoice's lines.

    The iPos sync writes ``item_id`` with a blank ``item_name``; the CPMS POS
    sometimes writes ``item_name`` with no FK (see migration 0106). Comparing on
    the *resolved display name* is the only way the same sale is recognisable
    from both sides.

    A ``frozenset`` of a ``Counter``'s items rather than a plain set, so an
    invoice with the same line twice cannot match one that has it once.
    """
    counts = defaultdict(int)
    for item in invoice.items.all():
        name = ((item.item.name if item.item_id else item.item_name) or '').strip().lower()
        counts[(name, D(item.quantity), D(item.price))] += 1
    return frozenset(counts.items())


class Command(BaseCommand):
    help = 'Void IPOS-* invoices that exactly duplicate a CPMS-native invoice.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply', action='store_true',
            help='Commit. Without it everything runs and is rolled back.',
        )
        parser.add_argument(
            '--start', default=PARALLEL_START.isoformat(),
            help=f'First day of the parallel run (default {PARALLEL_START}).',
        )
        parser.add_argument(
            '--end', default=PARALLEL_END.isoformat(),
            help=f'Last day of the parallel run (default {PARALLEL_END}).',
        )
        parser.add_argument(
            '--limit', type=int, default=0,
            help='Report at most N decisions per section (0 = all).',
        )

    # ── entry point ──────────────────────────────────────────────────────────

    def handle(self, *args, **opts):
        try:
            start = date.fromisoformat(opts['start'])
            end = date.fromisoformat(opts['end'])
        except ValueError as exc:
            raise CommandError(f'Bad date: {exc}') from exc
        if start > end:
            raise CommandError('--start must not be after --end.')

        w = self.stdout.write
        w(self.style.MIGRATE_HEADING(
            f'iPos duplicate invoices, {start} … {end}'
            + ('' if opts['apply'] else '  —  DRY RUN, rolled back at the end')
        ))

        with transaction.atomic():
            cms, imported = self._load(start, end)
            w('')
            w(f'  CPMS-native invoices in window : {len(cms):>5}  '
              f'Rp {sum(i.grand_total for i in cms):>18,.2f}')
            w(f'  iPos-imported invoices in window: {len(imported):>5}  '
              f'Rp {sum(i.grand_total for i in imported):>18,.2f}')

            decisions, stats = self._match(cms, imported)
            self._execute(decisions, opts['apply'])
            self._report(w, decisions, stats, opts['limit'])

            if not opts['apply']:
                transaction.set_rollback(True)
                w(self.style.WARNING(
                    '\nDRY RUN — nothing was written. Re-run with --apply to commit.'))
            else:
                w(self.style.SUCCESS('\nCommitted.'))

    # ── selection ────────────────────────────────────────────────────────────

    def _load(self, start, end):
        base = (
            Invoice.objects
            .filter(datetime__date__gte=start, datetime__date__lte=end, is_voided=False)
            .prefetch_related('items__item')
            # id is the tie-break: a POS run stamps a whole visit with one
            # datetime, and because a CPMS invoice may absorb only one twin the
            # order decides which iPos row gets voided. Never leave that to the
            # planner in a command that destroys revenue records.
            .order_by('datetime', 'id')
        )
        prefix = f'{IMPORTED_INVOICE_PREFIX}-'
        cms = [i for i in base if not (i.invoice_number or '').startswith(prefix)]
        imported = [i for i in base if (i.invoice_number or '').startswith(prefix)]
        return cms, imported

    def _match(self, cms, imported):
        """Greedy 1:1 match of iPos rows onto CPMS rows.

        Returns ``(decisions, stats)`` where a decision is
        ``(imported_invoice, cms_twin_or_None, verdict, note)``.
        """
        pool = defaultdict(list)
        for inv in cms:
            sig = line_signature(inv)
            if not sig:
                continue
            pool[(inv.datetime.date(), D(inv.grand_total), sig)].append(inv)

        decisions = []
        stats = defaultdict(int)
        stats['voided_amount'] = D('0')
        stats['kept_amount'] = D('0')

        for inv in imported:
            sig = line_signature(inv)
            if not sig:
                decisions.append((inv, None, 'no-lines', 'no line items — skipped'))
                stats['skipped'] += 1
                stats['kept_amount'] += inv.grand_total
                continue

            key = (inv.datetime.date(), D(inv.grand_total), sig)
            candidates = pool.get(key) or []
            if not candidates:
                decisions.append((
                    inv, None, 'keep',
                    'no CPMS twin — genuine iPos-only sale',
                ))
                stats['unique'] += 1
                stats['kept_amount'] += inv.grand_total
                continue

            twin = candidates.pop(0)          # consumes it: strictly 1:1
            note = ''
            if twin.patient_no_id != inv.patient_no_id:
                note = (f'patient differs ({inv.patient_no_id} vs {twin.patient_no_id}) '
                        f'— iPos sync created its own patient row')
                stats['patient_mismatch'] += 1
            decisions.append((inv, twin, 'void', note))
            stats['voided'] += 1
            stats['voided_amount'] += inv.grand_total

        return decisions, stats

    # ── mutation ─────────────────────────────────────────────────────────────

    def _execute(self, decisions, apply_changes):
        """Void every 'void' decision. Runs in dry-run too — the surrounding
        transaction is rolled back — so the report describes work that actually
        succeeded rather than work that was merely planned."""
        touched = set()
        now = timezone.now()

        for invoice, twin, verdict, _note in decisions:
            if verdict != 'void':
                continue

            self._reverse_journal(invoice)

            invoice.is_voided = True
            invoice.voided_at = now
            invoice.save(update_fields=['is_voided', 'voided_at'])

            AuditLog.objects.create(
                action='DELETE',
                entity_type='Invoice',
                entity_id=str(invoice.id),
                description=(
                    f'Invoice {invoice.invoice_number} voided as an iPos import that '
                    f'duplicates CPMS invoice {twin.invoice_number} — same date, total '
                    f'and line items during the 2026-06-02…18 parallel run'
                ),
            )
            if invoice.patient_no_id:
                touched.add(invoice.patient_no)

        for patient in touched:
            refresh_crm_profile(patient)

    def _reverse_journal(self, invoice):
        """Reverse any posted journal entries for ``invoice``.

        Normally a no-op: IPOS-* invoices are held out of the ledger by design.
        When a sweep has posted one anyway, each entry is negated by a reversing
        entry dated today rather than deleted, so the correction is visible in
        the journal instead of history quietly changing shape.
        """
        entries = (
            JournalEntry.objects
            .filter(invoice=invoice, source_type='invoice')
            .exclude(reversed_by__isnull=False)
        )
        today = timezone.localdate()
        for entry in entries:
            write_legs(
                reverse_legset(legset_from_entry(entry)),
                date=today,
                source_type='void_memo',
                document=invoice,
                memo=f'Pembatalan {invoice.invoice_number} — duplikat impor iPos',
                reverses=entry,
            )
        if invoice.posting_status == 'posted':
            # Leave it 'posted': the original entries and their reversals both
            # stand. Flipping it back to 'unposted' would invite the next sweep
            # to post the whole thing a third time.
            pass

    # ── reporting ────────────────────────────────────────────────────────────

    def _report(self, w, decisions, stats, limit):
        def rows(verdict):
            out = [d for d in decisions if d[2] == verdict]
            return out[:limit] if limit else out

        voided = rows('void')
        if voided:
            w('')
            w(self.style.MIGRATE_HEADING('Voided as duplicates'))
            for invoice, twin, _v, note in voided:
                w(f'  VOID  {invoice.invoice_number:<24} {invoice.datetime:%Y-%m-%d} '
                  f'{invoice.grand_total:>14,.2f}  = {twin.invoice_number}'
                  + (f'  ({note})' if note else ''))

        kept = rows('keep') + rows('no-lines')
        if kept:
            w('')
            w(self.style.MIGRATE_HEADING('Kept — not duplicates'))
            for invoice, _t, _v, note in kept:
                w(self.style.WARNING(
                    f'  KEEP  {invoice.invoice_number:<24} {invoice.datetime:%Y-%m-%d} '
                    f'{invoice.grand_total:>14,.2f}  {note}'))
            if limit and len(kept) >= limit:
                w(self.style.WARNING(f'  … truncated at --limit {limit}'))

        w('')
        w(self.style.MIGRATE_HEADING('Summary'))
        w(f'  voided as duplicates          : {stats["voided"]:,}')
        w(f'  revenue removed               : Rp {stats["voided_amount"]:,.2f}')
        w(f'  of those, patient_no differed : {stats["patient_mismatch"]:,}')
        w(f'  kept — no CPMS twin           : {stats["unique"]:,}')
        w(f'  kept — no line items          : {stats["skipped"]:,}')
        w(f'  kept value                    : Rp {stats["kept_amount"]:,.2f}')
        w('')
        w(self.style.WARNING(
            '  Reminder: the kept iPos invoices are genuine sales that were never\n'
            '  keyed into CPMS. Do not void them wholesale. Any residual gap against\n'
            '  the accountant after this run is partial re-keying inside CPMS itself\n'
            '  and needs a human, not a wider match rule.'
        ))
