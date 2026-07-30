"""Link the mis-spelled 'Inejksi Flek 500' invoice lines to their treatment.

15 InvoiceItem rows were billed by name with the letters transposed, so they never
matched a Treatment and their revenue fell back to 4200000 Product Sales Revenue
instead of 4410000 Treatment Revenue - Flek.

Only rows with no item FK are touched. 126 other rows carry the same misspelling in
``item_name`` but already have an ``item_id``; those route correctly through the FK
and their stale name text is harmless.

Re-run ``manage.py repair_accounting_balance --apply`` afterwards to move the revenue
onto the right account — this migration only corrects the line data.
"""
from django.db import migrations


MISSPELLED = 'Inejksi Flek 500'
TREATMENT_NAME = 'Injeksi Flek 500'


def link_lines(apps, schema_editor):
    InvoiceItem = apps.get_model('managementsys', 'InvoiceItem')
    Treatment = apps.get_model('managementsys', 'Treatment')

    treatment = Treatment.objects.filter(name=TREATMENT_NAME).first()
    if treatment is None or not treatment.catalog_item_id:
        return
    InvoiceItem.objects.filter(item__isnull=True, item_name=MISSPELLED).update(
        item_id=treatment.catalog_item_id,
        # item_name is only carried for lines with no FK; clear it to match the
        # convention the invoice views write.
        item_name='',
    )


def unlink_lines(apps, schema_editor):
    InvoiceItem = apps.get_model('managementsys', 'InvoiceItem')
    Treatment = apps.get_model('managementsys', 'Treatment')

    treatment = Treatment.objects.filter(name=TREATMENT_NAME).first()
    if treatment is None or not treatment.catalog_item_id:
        return
    # Best effort: this cannot distinguish these rows from lines that were always
    # linked, so it is deliberately a no-op rather than a wrong guess.
    return


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0076_undeposited_funds_account'),
    ]

    operations = [
        migrations.RunPython(link_lines, unlink_lines),
    ]
