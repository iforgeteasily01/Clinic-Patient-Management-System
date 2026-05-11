import datetime
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0013_chartofaccounts_seed'),
    ]

    operations = [
        migrations.CreateModel(
            name='PatientPhoto',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('photo_date', models.DateField(default=datetime.date.today)),
                ('body_area', models.CharField(max_length=100)),
                ('image', models.ImageField(upload_to='patient_photos/%Y/%m/%d/')),
                ('uploaded_at', models.DateTimeField(auto_now_add=True)),
                ('patient_no', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='photos', to='managementsys.patient')),
            ],
            options={
                'ordering': ['uploaded_at'],
            },
        ),
    ]
