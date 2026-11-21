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
        item_name='',
    )


def unlink_lines(apps, schema_editor):
    InvoiceItem = apps.get_model('managementsys', 'InvoiceItem')
    Treatment = apps.get_model('managementsys', 'Treatment')

    treatment = Treatment.objects.filter(name=TREATMENT_NAME).first()
    if treatment is None or not treatment.catalog_item_id:
        return
    return


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0076_undeposited_funds_account'),
    ]

    operations = [
        migrations.RunPython(link_lines, unlink_lines),
    ]
