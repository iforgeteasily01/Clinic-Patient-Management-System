import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


def backfill_live_visits(apps, schema_editor):
    ActivePatient = apps.get_model('managementsys', 'ActivePatient')
    TreatmentSession = apps.get_model('managementsys', 'TreatmentSession')
    BeauticianAssignment = apps.get_model('managementsys', 'BeauticianAssignment')
    for ap in ActivePatient.objects.filter(status=4):
        session = (TreatmentSession.objects
                   .filter(active_patient=ap, beautician__isnull=False)
                   .order_by('-session_time', '-id').first())
        if session is None:
            continue
        BeauticianAssignment.objects.create(
            active_patient=ap, patient_no_id=ap.patient_no_id, guest_name=ap.guest_name,
            beautician_id=session.beautician_id, branch_id=ap.branch_id,
            assigned_at=session.session_time,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0119_invoice_posting_excluded'),
    ]

    operations = [
        migrations.CreateModel(
            name='BeauticianAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('guest_name', models.CharField(blank=True, max_length=100, null=True)),
                ('assigned_at', models.DateTimeField(default=django.utils.timezone.now)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('reason', models.CharField(blank=True, default='', max_length=200)),
                ('active_patient', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='beautician_assignments', to='managementsys.activepatient')),
                ('assigned_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='managementsys.appuser')),
                ('beautician', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='assignments', to='managementsys.beauticians')),
                ('branch', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='beautician_assignments', to='managementsys.branch')),
                ('patient_no', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to='managementsys.patient')),
            ],
            options={
                'ordering': ['assigned_at', 'id'],
            },
        ),
        migrations.RunPython(backfill_live_visits, migrations.RunPython.noop),
    ]
