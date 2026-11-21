import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0100_retire_undeposited_clearing'),
    ]

    operations = [
        migrations.CreateModel(
            name='ReportSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('stock_fast_window_days', models.IntegerField(default=30)),
                ('stock_fast_top_percent', models.IntegerField(default=20)),
                ('stock_slow_months', models.IntegerField(default=2)),
                ('stock_dead_months', models.IntegerField(default=4)),
                ('patient_active_months', models.IntegerField(default=6)),
                ('patient_inactive_months', models.IntegerField(default=12)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Report Settings',
            },
        ),
        migrations.AddField(
            model_name='invoice',
            name='payment_account',
            field=models.ForeignKey(blank=True, help_text='Cash/bank account debited by this invoice. Must be one of services.cash_accounts.cash_bank_account_ids().', limit_choices_to={'account_type': 'asset'}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='invoices_received_into', to='managementsys.chartofaccounts'),
        ),
        migrations.CreateModel(
            name='ExpenseAlias',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=120)),
                ('scope', models.CharField(choices=[('beautician', 'Beautician'), ('general', 'General')], default='beautician', max_length=20)),
                ('is_active', models.BooleanField(default=True)),
                ('sort_order', models.IntegerField(default=0)),
                ('notes', models.CharField(blank=True, default='', max_length=255)),
                ('account', models.ForeignKey(limit_choices_to={'account_type__in': ['expense', 'cogs']}, on_delete=django.db.models.deletion.PROTECT, related_name='expense_aliases', to='managementsys.chartofaccounts')),
            ],
            options={
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.AddField(
            model_name='expense',
            name='source',
            field=models.CharField(choices=[('general', 'General'), ('beautician', 'Beautician')], db_index=True, default='general', max_length=20),
        ),
        migrations.AddField(
            model_name='expenseitem',
            name='alias',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expense_items', to='managementsys.expensealias'),
        ),
        migrations.AddConstraint(
            model_name='expensealias',
            constraint=models.UniqueConstraint(fields=('name', 'scope'), name='uniq_expense_alias_name_scope'),
        ),
    ]
