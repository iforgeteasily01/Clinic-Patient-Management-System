from django.db import migrations


def null_accounts(apps, schema_editor):
    TreatmentCategory = apps.get_model('managementsys', 'TreatmentCategory')
    TreatmentCategory.objects.exclude(cogs_account__isnull=True).update(cogs_account=None)
    TreatmentCategory.objects.exclude(expense_account__isnull=True).update(expense_account=None)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0084_journal_backfill'),
    ]

    operations = [
        migrations.RunPython(null_accounts, noop),
    ]
