"""Phase 2 cutover backfill.

Every date that already has LedgerEntry rows today was, in effect, "posted"
under the old synchronous-posting model — mark it as such in JournalDayLog so
the report-gating added in this phase doesn't retroactively block historical
reporting for data that predates the journal-run concept entirely.

Symmetrically, mark posting_status='posted' on every existing
Invoice/PurchaseInvoice/AccountTransfer that already has LedgerEntry rows, so
the first real journal run doesn't try to re-post (and double-count) work
that was already posted synchronously before this migration.

Documents with zero LedgerEntry rows (there shouldn't be many/any at
cutover time, since posting used to be synchronous) are left at the
'unposted' default and will be picked up by the first journal run.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    JournalDayLog = apps.get_model('managementsys', 'JournalDayLog')
    Invoice = apps.get_model('managementsys', 'Invoice')
    PurchaseInvoice = apps.get_model('managementsys', 'PurchaseInvoice')
    AccountTransfer = apps.get_model('managementsys', 'AccountTransfer')

    posted_dates = LedgerEntry.objects.values_list('date', flat=True).distinct()
    for d in posted_dates:
        JournalDayLog.objects.update_or_create(date=d, defaults={'is_posted': True})

    posted_invoice_ids = LedgerEntry.objects.filter(
        invoice__isnull=False
    ).values_list('invoice_id', flat=True).distinct()
    Invoice.objects.filter(id__in=list(posted_invoice_ids)).update(posting_status='posted')

    posted_purchase_ids = LedgerEntry.objects.filter(
        purchase_invoice__isnull=False
    ).values_list('purchase_invoice_id', flat=True).distinct()
    PurchaseInvoice.objects.filter(id__in=list(posted_purchase_ids)).update(posting_status='posted')

    posted_transfer_ids = LedgerEntry.objects.filter(
        transfer__isnull=False
    ).values_list('transfer_id', flat=True).distinct()
    AccountTransfer.objects.filter(id__in=list(posted_transfer_ids)).update(posting_status='posted')


def unbackfill(apps, schema_editor):
    # Irreversible by design — we cannot tell which JournalDayLog/posting_status
    # rows predate this migration vs. were legitimately set by a later journal
    # run. Down-migrating this is a no-op; reversing to a pre-Phase-2 state
    # would require restoring from a backup taken before this migration ran.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0083_accounttransfer_posting_status_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
