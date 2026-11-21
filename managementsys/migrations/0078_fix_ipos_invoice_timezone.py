from datetime import timedelta

from django.db import migrations
from django.db.models import F

IPOS_PREFIX = 'IPOS-'
OFFSET = timedelta(hours=7)


def shift_to_jakarta(apps, schema_editor):
    Invoice = apps.get_model('managementsys', 'Invoice')
    Invoice.objects.filter(
        invoice_number__startswith=IPOS_PREFIX,
        datetime__isnull=False,
    ).update(datetime=F('datetime') - OFFSET)


def shift_back(apps, schema_editor):
    Invoice = apps.get_model('managementsys', 'Invoice')
    Invoice.objects.filter(
        invoice_number__startswith=IPOS_PREFIX,
        datetime__isnull=False,
    ).update(datetime=F('datetime') + OFFSET)


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0077_fix_misspelled_injeksi_flek_lines'),
    ]

    operations = [
        migrations.RunPython(shift_to_jakarta, shift_back),
    ]
