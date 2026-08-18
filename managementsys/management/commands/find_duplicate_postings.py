"""Find documents whose journal postings were written more than once.

Read-only. Nothing here rewrites history — a genuinely double-posted document is
fixed with a correction journal (``POST /api/accounting/journal/entries/<pk>/
correct/``), which is why this command reports the entry number to correct rather
than offering to delete anything.

Two separate conditions produced duplicate-looking journals on an invoice, and
they need very different responses:

  DOUBLE-POSTED (severe)
    More than one source journal entry for the same document. Revenue is counted
    twice and stock was consumed twice. Caused by a commit posting an already-
    posted document — a double-clicked commit button, or two journal runs whose
    preview phases interleaved. Both are now blocked (journal_preview: the draft
    claim and the per-document ``select_for_update`` check), but anything already
    in the books stays there until corrected.

  REDUNDANT EDIT MEMOS (cosmetic)
    An edit-memo reversal + repost pair that nets to zero on every account,
    written by a PATCH that changed nothing the posting reads. Balances are
    correct; the rows are just noise on the invoice's journal view. Now
    prevented in InvoiceDetailView.put by comparing the GL fingerprint before
    and after the edit. Historic rows are left alone deliberately: deleting
    posted ledger rows is exactly the thing correction journals exist to avoid.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand

from managementsys.models import JournalEntry, LedgerEntry

# Source types a document produces exactly once. Memo types are excluded: an
# edit or void legitimately adds more rows to a document that was already posted.
SOURCE_TYPES = ('invoice', 'purchase', 'transfer', 'expense')

# LedgerEntry FK -> how to label the document it points at.
DOCUMENT_FIELDS = {
    'invoice':          'Invoice',
    'purchase_invoice': 'Purchase',
    'transfer':         'Transfer',
    'expense':          'Expense',
}


# Migration 0097 grouped the pre-Phase-4 flat ledger into synthetic headers,
# keyed by (date, source_type, document). A document that was edited back then
# re-posted under its own source_type on the edit's date, so it legitimately ends
# up with several of these — that is a record of history, not a double posting.
BACKFILL_PREFIX = 'JE-H'


def double_posted():
    """Documents carrying more than one source JournalEntry.

    Returns ``(live, historical)``, each ``[((kind, id), [JournalEntry, ...]), ...]``
    worst first. A group is historical when any of its entries came from the
    migration 0097 backfill; those need a human, not a correction journal.
    """
    by_document = defaultdict(list)
    entries = (
        JournalEntry.objects
        .filter(source_type__in=SOURCE_TYPES)
        .order_by('date', 'entry_number')
    )
    for entry in entries:
        for field, kind in DOCUMENT_FIELDS.items():
            doc_id = getattr(entry, f'{field}_id')
            if doc_id:
                by_document[(kind, doc_id)].append(entry)
                break

    live, historical = [], []
    for key, rows in by_document.items():
        if len(rows) < 2:
            continue
        bucket = (historical if any(e.entry_number.startswith(BACKFILL_PREFIX) for e in rows)
                  else live)
        bucket.append((key, rows))
    for bucket in (live, historical):
        bucket.sort(key=lambda kv: (-len(kv[1]), kv[0]))
    return live, historical


def redundant_edit_memos():
    """Invoices whose edit-memo rows net to zero on every account.

    A net-zero memo day is a reversal immediately followed by an identical
    repost — the signature of an edit that changed nothing. Grouped by day
    because memo rows carry no journal-entry header to group by.

    Returns ``[(invoice_id, day, row_count), ...]``.
    """
    rows = (
        LedgerEntry.objects
        .filter(source_type__in=('edit_memo', 'void_memo'), invoice__isnull=False)
        .values_list('invoice_id', 'date', 'account_id', 'entry_type', 'amount')
    )

    per_day = defaultdict(lambda: (defaultdict(Decimal), [0]))
    for invoice_id, day, account_id, entry_type, amount in rows:
        net, count = per_day[(invoice_id, day)]
        net[account_id] += amount if entry_type == 'debit' else -amount
        count[0] += 1

    out = [
        (invoice_id, day, count[0])
        for (invoice_id, day), (net, count) in per_day.items()
        if all(v == 0 for v in net.values())
    ]
    out.sort(key=lambda t: (-t[2], t[0]))
    return out


class Command(BaseCommand):
    help = 'Report documents whose journal postings were written more than once.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=40,
                            help='Rows to list per section (default 40).')

    def handle(self, *args, **opts):
        w = self.stdout.write
        limit = opts['limit']

        w(self.style.MIGRATE_HEADING('Duplicate journal postings'))

        def listing(rows):
            return ', '.join(
                f'{e.entry_number} ({e.date}, pk={e.pk}, Dr {e.total_debit})' for e in rows)

        dupes, historical = double_posted()
        if not dupes:
            w(self.style.SUCCESS('  [ok] No document has more than one live source journal entry.'))
        else:
            extra = sum(len(rows) - 1 for _k, rows in dupes)
            w(self.style.ERROR(
                f'  [!!] {len(dupes)} document(s) posted more than once '
                f'({extra} surplus entr{"y" if extra == 1 else "ies"}).'))
            w('       Correct each surplus entry via POST '
              '/api/accounting/journal/entries/<pk>/correct/')
            for (kind, doc_id), rows in dupes[:limit]:
                w(f'       {kind} #{doc_id}: {listing(rows)}')
            if len(dupes) > limit:
                w(f'       … and {len(dupes) - limit} more (raise --limit to see them).')

        if historical:
            w('')
            w(self.style.WARNING(
                f'  [--] {len(historical)} document(s) with several {BACKFILL_PREFIX}-* entries.'))
            w('       Pre-Phase-4 history reconstructed by migration 0097: a document edited '
              'back then re-posted under its own source type on the edit date, so several '
              'headers is expected. Not a double posting; verify before touching.')
            for (kind, doc_id), rows in historical[:limit]:
                w(f'       {kind} #{doc_id}: {listing(rows)}')
            if len(historical) > limit:
                w(f'       … and {len(historical) - limit} more (raise --limit to see them).')

        w('')
        memos = redundant_edit_memos()
        if not memos:
            w(self.style.SUCCESS('  [ok] No net-zero edit/void memo groups.'))
        else:
            total_rows = sum(c for _i, _d, c in memos)
            w(self.style.WARNING(
                f'  [--] {len(memos)} net-zero memo group(s) across '
                f'{len({i for i, _d, _c in memos})} invoice(s), {total_rows} ledger rows.'))
            w('       Cosmetic only — balances are correct. These are the rows that made '
              'an invoice look journalled several times over.')
            for invoice_id, day, count in memos[:limit]:
                w(f'       Invoice #{invoice_id} on {day}: {count} rows netting to zero')
            if len(memos) > limit:
                w(f'       … and {len(memos) - limit} more (raise --limit to see them).')

        # Non-zero exit only for the condition that actually misstates the books.
        if dupes:
            raise SystemExit(1)
