import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0086_remove_treatmentcategory_cogs_expense'),
    ]

    operations = [
        migrations.CreateModel(
            name='Expense',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('expense_date', models.DateField()),
                ('payee', models.CharField(max_length=255)),
                ('status', models.CharField(choices=[('unpaid', 'Belum Dibayar'), ('partial', 'Sebagian Dibayar'), ('paid', 'Lunas')], default='unpaid', max_length=10)),
                ('total_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('amount_paid', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('notes', models.TextField(blank=True)),
                ('posting_status', models.CharField(choices=[('unposted', 'Unposted'), ('posted', 'Posted')], default='unposted', max_length=10)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expenses', to='managementsys.appuser')),
                ('payment_method', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expenses', to='managementsys.paymentmethod')),
            ],
            options={
                'ordering': ['-expense_date', '-created_at'],
            },
        ),
        migrations.CreateModel(
            name='ExpenseItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('description', models.CharField(blank=True, max_length=255)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('account', models.ForeignKey(limit_choices_to={'account_type__in': ['expense', 'cogs']}, on_delete=django.db.models.deletion.PROTECT, related_name='expense_items', to='managementsys.chartofaccounts')),
                ('expense', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='managementsys.expense')),
            ],
            options={
                'ordering': ['id'],
            },
        ),
        migrations.AddField(
            model_name='ledgerentry',
            name='expense',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='ledger_entries', to='managementsys.expense'),
        ),
        migrations.AlterField(
            model_name='ledgerentry',
            name='source_type',
            field=models.CharField(blank=True, choices=[('invoice', 'Sales Invoice'), ('purchase', 'Purchase Invoice'), ('transfer', 'Account Transfer'), ('adjustment', 'Manual Adjustment'), ('stock', 'Stock Movement'), ('opname', 'Stock Opname'), ('manual', 'Manual Entry'), ('void_memo', 'Void Memo (reversal)'), ('edit_memo', 'Edit Memo (reversal + repost)'), ('expense', 'Expense')], default='', max_length=15),
        ),
    ]
