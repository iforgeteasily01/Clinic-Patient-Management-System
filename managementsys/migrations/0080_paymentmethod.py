"""Introduce the PaymentMethod model.

Phase 1 of the accounting redesign: "how the customer paid" becomes its own
entity instead of being a direct FK to ChartOfAccounts. This migration only
adds the new model and new (nullable, temporary) FK columns alongside the
existing ones — 0081 backfills PaymentMethod rows and repoints the values,
and 0082 drops the old columns / renames the new ones into place.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0079_coa_backfill_orphan_parents'),
    ]

    operations = [
        migrations.CreateModel(
            name='PaymentMethod',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('code', models.CharField(blank=True, default='', max_length=30)),
                ('is_active', models.BooleanField(default=True)),
                ('is_system', models.BooleanField(default=False)),
                ('sort_order', models.IntegerField(default=0)),
                ('linked_account', models.ForeignKey(
                    limit_choices_to={'account_type': 'asset'},
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='payment_methods',
                    to='managementsys.chartofaccounts',
                )),
            ],
            options={
                'verbose_name': 'Payment Method',
                'verbose_name_plural': 'Payment Methods',
                'ordering': ['sort_order', 'name'],
            },
        ),
        migrations.AddField(
            model_name='invoice',
            name='payment_method_new',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='invoices',
                to='managementsys.paymentmethod',
            ),
        ),
        migrations.AddField(
            model_name='purchaseinvoice',
            name='payment_method_new',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='purchase_invoices',
                to='managementsys.paymentmethod',
            ),
        ),
    ]
