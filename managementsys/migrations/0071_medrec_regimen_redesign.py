from django.db import migrations, models


def _combine(a, b):
    a = (a or '').strip()
    b = (b or '').strip()
    if a and b:
        return a if a == b else f"{a} / {b}"
    return a or b


def forwards(apps, schema_editor):
    MedRec = apps.get_model('managementsys', 'MedRec')
    for rec in MedRec.objects.all().iterator():
        rec.sabun = _combine(rec.sabun_pagi, rec.sabun_malam)
        rec.toner = _combine(rec.toner_pagi, rec.toner_malam)
        rec.save(update_fields=['sabun', 'toner'])


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0070_siteconfig_logo'),
    ]

    operations = [
        migrations.AddField(
            model_name='medrec',
            name='sabun',
            field=models.TextField(default='', null=True),
        ),
        migrations.AddField(
            model_name='medrec',
            name='toner',
            field=models.TextField(default='', null=True),
        ),
        migrations.AddField(
            model_name='medrec',
            name='obat1_detail',
            field=models.TextField(default='', null=True),
        ),
        migrations.AddField(
            model_name='medrec',
            name='obat2_detail',
            field=models.TextField(default='', null=True),
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name='medrec', name='sabun_pagi'),
        migrations.RemoveField(model_name='medrec', name='sabun_malam'),
        migrations.RemoveField(model_name='medrec', name='toner_pagi'),
        migrations.RemoveField(model_name='medrec', name='toner_malam'),
    ]
