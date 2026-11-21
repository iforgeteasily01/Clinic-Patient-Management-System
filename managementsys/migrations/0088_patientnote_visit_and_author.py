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
