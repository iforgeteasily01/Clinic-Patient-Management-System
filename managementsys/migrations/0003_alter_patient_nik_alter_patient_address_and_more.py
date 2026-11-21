from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0002_patient_nik'),
    ]

    operations = [
        migrations.AlterField(
            model_name='patient',
            name='NIK',
            field=models.CharField(max_length=16, null=True),
        ),
        migrations.AlterField(
            model_name='patient',
            name='address',
            field=models.CharField(max_length=100, null=True),
        ),
        migrations.AlterField(
            model_name='patient',
            name='name',
            field=models.CharField(max_length=100, null=True),
        ),
        migrations.AlterField(
            model_name='patient',
            name='phone_number',
            field=models.CharField(max_length=15, null=True),
        ),
    ]
