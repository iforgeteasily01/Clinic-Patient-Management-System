from django.db import migrations, models


def forwards(apps, schema_editor):
    MedRec = apps.get_model('managementsys', 'MedRec')
    for rec in MedRec.objects.all().iterator():
        rec.obat1_pagi_detail = rec.obat1_detail or ''
        rec.obat2_pagi_detail = rec.obat2_detail or ''
        rec.save(update_fields=['obat1_pagi_detail', 'obat2_pagi_detail'])


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0071_medrec_regimen_redesign'),
    ]

    operations = [
        migrations.AddField(
            model_name='medrec',
            name='obat1_pagi_detail',
            field=models.TextField(default='', null=True),
        ),
        migrations.AddField(
            model_name='medrec',
            name='obat1_malam_detail',
            field=models.TextField(default='', null=True),
        ),
        migrations.AddField(
            model_name='medrec',
            name='obat2_pagi_detail',
            field=models.TextField(default='', null=True),
        ),
        migrations.AddField(
            model_name='medrec',
            name='obat2_malam_detail',
            field=models.TextField(default='', null=True),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name='medrec', name='obat1_detail'),
        migrations.RemoveField(model_name='medrec', name='obat2_detail'),
    ]
