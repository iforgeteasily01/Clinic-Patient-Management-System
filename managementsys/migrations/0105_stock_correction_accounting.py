"""Make a stock correction a journal document.

Until now ``StockOutLog`` recorded that stock left the building and nothing
else. ``StockOutView`` computed the FIFO cost of every issue and assigned it to
``_cogs``, an underscore-prefixed throwaway. So damaged, expired, mis-keyed and
internally-consumed inventory came off the balance sheet with no matching charge
anywhere in the P&L — the single largest reason CPMS reported Rp 1.551.290 of
cost of sales for June 2026 against the accountant's Rp 130.846.303.

This migration adds the three columns that turn the log into a document the
journal sweep can post:

``reason``          why the stock left, which is what selects the GL account.
``value``           the FIFO cost consumed, captured at deduction time.
``posting_status``  the same unposted → posted lifecycle as Invoice/Expense.

plus ``LedgerEntry.stock_out_log`` / ``JournalEntry.stock_out_log`` so a posted
correction reaches its source document in one hop, exactly like every other
document kind.

BACKFILL
--------
Existing rows get a reason inferred from the free-text ``notes`` staff already
wrote ('Pindah Gudang', 'Expired', 'Salah input', 'Kirim ke Kirana', …). They do
NOT get a value: the batches they drew on have been consumed, restocked and
re-consumed many times since, so any figure reconstructed now would be fiction.
They are stamped ``posted`` so the first sweep after this migration does not try
to journal historic movements at a cost of zero.

That is a deliberate one-way door. The June and July 2026 write-offs stay absent
from the ledger and must be entered as manual adjustments if the clinic wants
them; from this migration forward every issue carries its own cost.

ACCOUNTS
--------
Nothing is created here. Every account ``StockOutLog.REASON_ACCOUNTS`` points at
is already seeded by 0094 (5000900 Koreksi/Obat Rusak/ED, 6100005 BHP Ruang
Facial, 6100030 Obat Kirana, 6300010 Biaya Promosi). This migration only checks
they are present and reports the ones that are not, because a missing account
turns into a pending journal leg the operator has to resolve by hand.
"""
from django.db import migrations, models
import django.db.models.deletion


# Substrings matched case-insensitively against StockOutLog.notes, most specific
# first. Order matters: 'salah input' must beat 'input', and 'kirana' must be
# tested before the generic transfer words in case a note says both.
NOTE_PATTERNS = [
    ('kirana',         'kirana'),
    ('salah input',    'data_entry'),
    ('salah',          'data_entry'),
    ('koreksi',        'data_entry'),
    ('expired',        'expired'),
    ('kadaluarsa',     'expired'),
    ('kedaluwarsa',    'expired'),
    ('ed ',            'expired'),
    ('rusak',          'damaged'),
    ('pecah',          'damaged'),
    ('hilang',         'lost'),
    ('selisih',        'lost'),
    ('pindah gudang',  'transfer'),
    ('pindah',         'transfer'),
    ('transfer',       'transfer'),
    ('mutasi',         'transfer'),
    ('sampel',         'sample'),
    ('sample',         'sample'),
    ('tester',         'sample'),
    ('pemakaian',      'internal_use'),
    ('pakai',          'internal_use'),
]

REQUIRED_ACCOUNTS = [
    (5000900, 'Koreksi/Obat Rusak/ED/dr. Melia'),
    (6100005, 'Barang Habis Pakai Ruang Facial (Obat)'),
    (6100030, 'Obat Kirana'),
    (6300010, 'Biaya Promosi/Iklan'),
]


def classify(notes):
    """Best-effort reason for one historic row. 'other' when nothing matches."""
    text = (notes or '').strip().lower()
    if not text:
        return 'other'
    for needle, reason in NOTE_PATTERNS:
        if needle in text:
            return reason
    return 'other'


def backfill(apps, schema_editor):
    StockOutLog = apps.get_model('managementsys', 'StockOutLog')
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')

    missing = [
        f'{number} {name}'
        for number, name in REQUIRED_ACCOUNTS
        if not ChartOfAccounts.objects.filter(account_number=number).exists()
    ]
    if missing:
        print(
            '\n  ! Stock-correction accounts not found in the Chart of Accounts:\n'
            + ''.join(f'      {m}\n' for m in missing)
            + '    Corrections mapped to them will post as pending legs until they exist.\n'
              '    Re-run migration 0094 or create them by hand.'
        )

    # Bucket by reason so this is a handful of UPDATEs rather than one per row.
    by_reason = {}
    for pk, notes in StockOutLog.objects.values_list('id', 'notes'):
        by_reason.setdefault(classify(notes), []).append(pk)

    for reason, ids in by_reason.items():
        for chunk_start in range(0, len(ids), 500):
            StockOutLog.objects.filter(
                id__in=ids[chunk_start:chunk_start + 500]
            ).update(reason=reason)

    # Historic rows carry no recoverable cost — see the module docstring. Marked
    # posted so the next sweep does not journal them at zero.
    StockOutLog.objects.update(posting_status='posted')


def unbackfill(apps, schema_editor):
    # Nothing to undo: the columns themselves are removed by the schema
    # operations this RunPython sits behind.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0104_alter_journalentry_source_type_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='stockoutlog',
            name='reason',
            field=models.CharField(
                choices=[
                    ('transfer', 'Pindah Gudang'),
                    ('expired', 'Kedaluwarsa (ED)'),
                    ('damaged', 'Rusak'),
                    ('lost', 'Hilang/Selisih'),
                    ('data_entry', 'Koreksi Salah Input'),
                    ('internal_use', 'Pemakaian Internal Klinik'),
                    ('kirana', 'Kirim ke Kirana'),
                    ('sample', 'Sampel/Tester'),
                    ('other', 'Lain-lain'),
                ],
                default='other', max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='stockoutlog',
            name='value',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=18),
        ),
        migrations.AddField(
            model_name='stockoutlog',
            name='posting_status',
            field=models.CharField(
                choices=[('unposted', 'Unposted'), ('posted', 'Posted')],
                default='unposted', max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='ledgerentry',
            name='stock_out_log',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='ledger_entries',
                to='managementsys.stockoutlog',
            ),
        ),
        migrations.AddField(
            model_name='journalentry',
            name='stock_out_log',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='journal_entries',
                to='managementsys.stockoutlog',
            ),
        ),
        migrations.RunPython(backfill, unbackfill),
    ]
