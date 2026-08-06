"""Widen PatientNote so walk-in guests can have notes and every note can name
its author.

  * patient_no becomes nullable (guests have no Patient row)
  * active_patient FK added — the visit a guest note belongs to
  * author_user FK + author_role snapshot added for attribution
  * date gains an index, plus a composite (patient_no, date) index — both list
    endpoints and the billing-queue embed filter on exactly those columns
  * CheckConstraint: a note must have a patient OR a visit

Existing rows all have a patient_no, so the AlterField and the constraint are
satisfied without any backfill. author_user cannot be reconstructed for legacy
rows (only the free-text `author` was ever recorded) and is left NULL; the
serializer falls back to `author` for display.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0087_expense_expenseitem'),
    ]

    operations = [
        migrations.AlterField(
            model_name='patientnote',
            name='patient_no',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='notes', to='managementsys.patient',
            ),
        ),
        migrations.AddField(
            model_name='patientnote',
            name='active_patient',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='notes', to='managementsys.activepatient',
            ),
        ),
        migrations.AddField(
            model_name='patientnote',
            name='author_user',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='patient_notes', to='managementsys.appuser',
            ),
        ),
        migrations.AddField(
            model_name='patientnote',
            name='author_role',
            field=models.CharField(blank=True, default='', max_length=20),
        ),
        migrations.AlterField(
            model_name='patientnote',
            name='date',
            field=models.DateField(db_index=True),
        ),
        migrations.AddIndex(
            model_name='patientnote',
            index=models.Index(fields=['patient_no', 'date'], name='patientnote_pat_date_idx'),
        ),
        migrations.AddConstraint(
            model_name='patientnote',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(patient_no__isnull=False)
                    | models.Q(active_patient__isnull=False)
                ),
                name='patientnote_has_subject',
            ),
        ),
    ]
