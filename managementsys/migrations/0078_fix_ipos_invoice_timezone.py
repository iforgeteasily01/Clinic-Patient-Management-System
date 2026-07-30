"""Correct the timezone of invoices imported from the legacy iPos database.

The first run of ``import_ipos_data`` saved ``tbl_ikhd.tanggal`` as a naive
datetime, so Django stamped it as UTC.  iPos actually stores Jakarta local
time (GMT+7), which means every imported invoice is 7 hours ahead of the
instant it really happened.  ``import_ipos_data`` now attaches ``TZ_JAKARTA``
before saving, so this only affects rows already in the database.

Shift them back by 7 hours.  Only ``IPOS-`` invoices are touched; natively
created invoices (``INV-``) were always saved timezone-aware.

Verified before writing: all 25,324 imported invoices fall in the 09:00-20:59
UTC band (Jakarta clinic hours read as UTC), so no row was imported after the
fix and no row crosses a date boundary when shifted.
"""

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
