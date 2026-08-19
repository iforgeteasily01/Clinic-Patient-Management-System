"""Operational input templates + recorded period entries.

Planning-only tables: nothing here is a journal document. See
``views/admin_operations_page.py`` for why that boundary matters.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0108_taxrule_taxrulebracket_taxrulecomponent'),
    ]

    operations = [
        migrations.CreateModel(
            name='OperationalInputTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('category', models.CharField(blank=True, default='', max_length=80)),
                ('frequency', models.CharField(choices=[('monthly', 'Bulanan'), ('weekly', 'Mingguan')], default='monthly', max_length=10)),
                ('due_day', models.PositiveSmallIntegerField(default=1, help_text='Bulanan: tanggal 1–31. Mingguan: hari 1 (Senin) – 7 (Minggu).')),
                ('expected_amount', models.DecimalField(decimal_places=2, default=0, help_text='Perkiraan nominal; dipakai sebagai nilai awal saat input.', max_digits=18)),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.IntegerField(default=0)),
                ('notes', models.CharField(blank=True, default='', max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(blank=True, help_text='Akun beban yang diharapkan menampung biaya ini. Hanya untuk pelaporan.', limit_choices_to={'account_type__in': ['expense', 'cogs']}, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='operational_input_templates', to='managementsys.chartofaccounts')),
            ],
            options={
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.CreateModel(
            name='OperationalInputEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('period_key', models.CharField(max_length=10)),
                ('period_start', models.DateField()),
                ('amount', models.DecimalField(decimal_places=2, default=0, max_digits=18)),
                ('notes', models.CharField(blank=True, default='', max_length=255)),
                ('recorded_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('recorded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='operational_input_entries', to='managementsys.appuser')),
                ('template', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='entries', to='managementsys.operationalinputtemplate')),
            ],
            options={
                'ordering': ['-period_start', 'template__sort_order'],
            },
        ),
        migrations.AddConstraint(
            model_name='operationalinputtemplate',
            constraint=models.UniqueConstraint(fields=('name',), name='uniq_operational_input_template_name'),
        ),
        migrations.AddConstraint(
            model_name='operationalinputentry',
            constraint=models.UniqueConstraint(fields=('template', 'period_key'), name='uniq_operational_input_period'),
        ),
        migrations.AddIndex(
            model_name='operationalinputentry',
            index=models.Index(fields=['period_start'], name='idx_opinput_period_start'),
        ),
    ]
