from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0021_treatmentcategory_revenue_account'),
    ]

    operations = [
        migrations.CreateModel(
            name='PatientNote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('content', models.TextField()),
                ('author', models.CharField(blank=True, default='', max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('patient_no', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='notes',
                    to='managementsys.patient',
                )),
            ],
            options={
                'ordering': ['-date', '-created_at'],
            },
        ),
    ]
