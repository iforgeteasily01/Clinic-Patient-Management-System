from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0038_cash_head_promote'),
        ('managementsys', '0036_patient_updated_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='SiteConfig',
            fields=[
                ('id', models.AutoField(primary_key=True, serialize=False)),
                ('clinic_name', models.CharField(default='', max_length=200)),
                ('address_line1', models.CharField(default='', max_length=200)),
                ('address_line2', models.CharField(default='', max_length=200)),
                ('phone_fax', models.CharField(default='', max_length=200)),
                ('receipt_header_extra', models.TextField(default='')),
                ('receipt_footer', models.TextField(default='Terima kasih atas kunjungan Anda')),
            ],
            options={
                'verbose_name': 'Site Configuration',
            },
        ),
    ]
