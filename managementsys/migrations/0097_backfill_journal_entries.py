"""Backfill JournalEntry headers over the historic flat ledger.

Before Phase 4 a "journal entry" was not an object — the sweep wrote loose
LedgerEntry rows tagged with a source document FK. This migration reconstructs
the headers after the fact so every existing ledger line belongs to an entry and
the new detail page / correction flow works on historic data, not just on
whatever is posted from today on.

GROUPING KEY
============
    (date, source_type, invoice_id, purchase_invoice_id, transfer_id, expense_id)

That is exactly the tuple the posting code held constant while writing one
document's legs. Rows with no document FK at all — manual adjustments, and any
'manual'/'stock'/'opname' rows — cannot be grouped that way (two unrelated
adjustments on the same day would merge), so each such line becomes its own
single-line entry.

UNBALANCED HISTORIC GROUPS
==========================
Some historic groups will not balance: the pre-Phase-2 synchronous path could
post a partial block if an account lookup returned None, and voids/edits wrote
reversal rows under the same key. Those groups are NOT dropped and NOT repaired
— they are written with is_balanced=False so an operator can find them via the
entries list. Silently fixing accounting data in a migration would be worse than
surfacing it.

Idempotent: only LedgerEntry rows with journal_entry_id IS NULL are considered,
so a re-run after a partial failure resumes.
"""

from collections import OrderedDict, defaultdict
from decimal import Decimal

from django.db import migrations

CHUNK = 5000

# Entry numbers minted here are distinguishable from live ones on sight, so a
# reconstructed header is never mistaken for one the system actually numbered
# at posting time.
BACKFILL_PREFIX = 'JE-H'


def _label(source_type):
    return {
        'invoice':    'Faktur penjualan',
        'purchase':   'Faktur pembelian',
        'transfer':   'Transfer antar rekening',
        'expense':    'Beban operasional',
        'adjustment': 'Penyesuaian manual',
        'void_memo':  'Memo pembatalan',
        'edit_memo':  'Memo perubahan',
        'stock':      'Mutasi persediaan',
        'opname':     'Stock opname',
        'manual':     'Entri manual',
    }.get(source_type, 'Jurnal')


def forwards(apps, schema_editor):
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    JournalEntry = apps.get_model('managementsys', 'JournalEntry')
    JournalEntrySequence = apps.get_model('managementsys', 'JournalEntrySequence')

    qs = (
        LedgerEntry.objects
        .filter(journal_entry__isnull=True)
        .order_by('date', 'id')
        .values(
            'id', 'date', 'source_type', 'description', 'entry_type', 'amount',
            'invoice_id', 'purchase_invoice_id', 'transfer_id', 'expense_id',
        )
    )
    if not qs.exists():
        return

    # groups: key -> {'rows': [...], 'date':, 'source_type':, fks...}
    groups = OrderedDict()
    standalone = []

    for row in qs.iterator(chunk_size=CHUNK):
        has_doc = any(row[f] for f in
                      ('invoice_id', 'purchase_invoice_id', 'transfer_id', 'expense_id'))
        if not has_doc:
            standalone.append(row)
            continue
        key = (
            row['date'], row['source_type'], row['invoice_id'],
            row['purchase_invoice_id'], row['transfer_id'], row['expense_id'],
        )
        groups.setdefault(key, []).append(row)

    # Sort standalone rows into the same date order so numbering stays monotonic.
    per_year_count = defaultdict(int)

    def next_number(d):
        per_year_count[d.year] += 1
        return f'{BACKFILL_PREFIX}-{d.year}-{per_year_count[d.year]:06d}'

    pending_headers = []   # JournalEntry instances awaiting bulk_create
    line_assignments = []  # (entry_index_in_pending, [ledger_ids])

    def stage(rows, date, source_type, fks):
        debit = sum((Decimal(r['amount']) for r in rows if r['entry_type'] == 'debit'), Decimal('0'))
        credit = sum((Decimal(r['amount']) for r in rows if r['entry_type'] == 'credit'), Decimal('0'))
        memo = rows[0]['description'][:255] if rows else _label(source_type)
        header = JournalEntry(
            entry_number=next_number(date),
            date=date,
            memo=memo,
            source_type=source_type or '',
            invoice_id=fks[0],
            purchase_invoice_id=fks[1],
            transfer_id=fks[2],
            expense_id=fks[3],
            total_debit=debit,
            total_credit=credit,
            is_balanced=(debit == credit),
        )
        pending_headers.append(header)
        line_assignments.append([r['id'] for r in rows])

    for key, rows in groups.items():
        date, source_type, inv, pur, trf, exp = key
        stage(rows, date, source_type, (inv, pur, trf, exp))

    for row in standalone:
        stage([row], row['date'], row['source_type'], (None, None, None, None))

    # Write headers, then point the lines at them. bulk_create returns objects
    # with PKs on PostgreSQL, which is what the project runs.
    for start in range(0, len(pending_headers), CHUNK):
        window = pending_headers[start:start + CHUNK]
        created = JournalEntry.objects.bulk_create(window, batch_size=1000)
        for offset, entry in enumerate(created):
            ledger_ids = line_assignments[start + offset]
            LedgerEntry.objects.filter(id__in=ledger_ids).update(journal_entry_id=entry.pk)

    # Seed the live sequence past nothing — backfilled numbers use their own
    # prefix, so the JE- series still starts at 1. The row is created here only
    # so the first real posting does not race two workers into creating it.
    years = {h.date.year for h in pending_headers}
    for year in years:
        JournalEntrySequence.objects.get_or_create(year=year, defaults={'last_number': 0})


def backwards(apps, schema_editor):
    """Detach the lines and drop only the headers this migration created."""
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    JournalEntry = apps.get_model('managementsys', 'JournalEntry')

    backfilled = JournalEntry.objects.filter(entry_number__startswith=BACKFILL_PREFIX)
    LedgerEntry.objects.filter(journal_entry__in=backfilled).update(journal_entry=None)
    backfilled.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0096_journal_entry_and_staging'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
