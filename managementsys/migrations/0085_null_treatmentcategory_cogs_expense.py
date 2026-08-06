"""Phase 3 step 1/2: null out TreatmentCategory.cogs_account / expense_account
before the schema migration (0086) removes the fields outright. Per-category
COGS/expense accounts are being retired in favor of the new Expense/
ExpenseItem model, which posts directly to a ChartOfAccounts row chosen per
line rather than one fixed per category.

This is a data migration only — the orphaned ChartOfAccounts rows themselves
are left in place (they may still have historical LedgerEntry rows against
them) and are simply no longer referenced by TreatmentCategory afterward.
"""
from django.db import migrations


def null_accounts(apps, schema_editor):
    TreatmentCategory = apps.get_model('managementsys', 'TreatmentCategory')
    TreatmentCategory.objects.exclude(cogs_account__isnull=True).update(cogs_account=None)
    TreatmentCategory.objects.exclude(expense_account__isnull=True).update(expense_account=None)


def noop(apps, schema_editor):
    # Irreversible by design — we don't know which accounts were linked before.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0084_journal_backfill'),
    ]

    operations = [
        migrations.RunPython(null_accounts, noop),
    ]
