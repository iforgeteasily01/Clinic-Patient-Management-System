from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='NIK',
            field=models.CharField(default='', max_length=16),
            preserve_default=False,
        ),
    ]
