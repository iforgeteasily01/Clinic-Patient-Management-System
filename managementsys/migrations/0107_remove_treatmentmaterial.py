"""Remove the TreatmentMaterial bill-of-materials.

WHY
---
A treatment's cost was derived from a fixed recipe: N ml of cleanser, N units of
anaesthetic per session. ``build_invoice_legs`` walked that recipe on every
service line, consumed the FIFO stock and posted the result as COGS.

The clinic's position is that the recipe cannot be made accurate. Real usage
varies by patient, by operator and by session, so a fixed per-treatment quantity
produces a cost figure that is precise, automatic, and wrong — and being wrong in
the ledger is worse than being absent, because it silently misstates gross margin
per category and nobody has a reason to look.

Cost of sales for treatments is therefore entered by hand from now on, as a
periodic journal against 5000200 "Pemakaian Obat Treatment" (seeded by migration
0094). Product lines are unaffected: they still consume real FIFO batches on sale
and post real COGS to 5100000, because the quantity sold *is* the quantity used.

WHAT WENT WITH IT
-----------------
* ``TreatmentMaterial`` model and its table
* the deduction in ``invoice_page.build_invoice_legs`` and its mirror in
  ``_reverse_accounting_instances``
* ``TreatmentMaterialSerializer``, both admin views, and the two
  ``/api/admin/treatments/<pk>/materials/`` routes
* the materials editor in ``TreatmentsAdmin.tsx``
* ``tests/test_invoice_edit.py::TestServiceMaterialReversal``

STOCK IMPACT
------------
None going forward, and none retrospectively. Service lines no longer draw on
inventory, so from here batches only move on product sales, purchases and stock
corrections. Historic deductions already made are left exactly where they are —
they are real movements of real stock, and reversing them now would credit back
inventory that was genuinely used.

IRREVERSIBLE
------------
The reverse migration recreates the empty table so the schema can be rewound, but
the rows are gone. That is safe here because the table was empty at the time this
was written (0 rows across all 269 treatments in the 2026-07-31 snapshot, which
is itself why the feature was never producing any cost). The forward step prints
the count it is about to delete — if that number is not zero on your database,
stop and export the table before continuing.
"""
from django.db import migrations, models
import django.db.models.deletion


def report_before_delete(apps, schema_editor):
    TreatmentMaterial = apps.get_model('managementsys', 'TreatmentMaterial')
    count = TreatmentMaterial.objects.count()
    if count:
        print(
            f'\n  ! Dropping TreatmentMaterial with {count} rows. Recipes are not '
            f'recoverable after this migration.'
        )
    else:
        print('\n  TreatmentMaterial was empty (0 rows) — nothing lost.')


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0106_backfill_invoiceitem_treatment_link'),
    ]

    operations = [
        migrations.RunPython(report_before_delete, noop),
        # unique_together names both FKs, so it has to go before they do.
        migrations.AlterUniqueTogether(name='treatmentmaterial', unique_together=set()),
        migrations.RemoveField(model_name='treatmentmaterial', name='item'),
        migrations.RemoveField(model_name='treatmentmaterial', name='treatment'),
        migrations.DeleteModel(name='TreatmentMaterial'),
    ]
