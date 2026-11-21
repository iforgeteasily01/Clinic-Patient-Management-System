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
        migrations.AlterUniqueTogether(name='treatmentmaterial', unique_together=set()),
        migrations.RemoveField(model_name='treatmentmaterial', name='item'),
        migrations.RemoveField(model_name='treatmentmaterial', name='treatment'),
        migrations.DeleteModel(name='TreatmentMaterial'),
    ]
