import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0109_operational_input_templates'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='birth_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='patient',
            name='gender',
            field=models.CharField(blank=True, choices=[('F', 'Perempuan'), ('M', 'Laki-laki')], default='', max_length=1),
        ),
        migrations.CreateModel(
            name='MessageTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('category', models.CharField(choices=[('followup', 'Follow-up Kunjungan'), ('birthday', 'Ulang Tahun'), ('reminder', 'Pengingat Jadwal'), ('promo', 'Promosi'), ('other', 'Lainnya')], default='followup', max_length=20)),
                ('body', models.TextField()),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.IntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='message_templates', to='managementsys.appuser')),
            ],
            options={
                'ordering': ['sort_order', 'name'],
            },
        ),
    ]
