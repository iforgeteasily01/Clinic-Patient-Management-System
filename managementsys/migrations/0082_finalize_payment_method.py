"""Finalize the PaymentMethod cut-over.

Drops the old direct-to-ChartOfAccounts columns (Invoice.payment_method,
PurchaseInvoice.payment_account) now that 0081 has backfilled and repointed
every row through PaymentMethod, then renames the temporary
``payment_method_new`` columns into their permanent names.
PurchaseInvoice.payment_method is required (mirrors the old payment_account,
which was never nullable), so it is tightened to NOT NULL once renamed.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0081_backfill_payment_methods'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='invoice',
            name='payment_method',
        ),
        migrations.RenameField(
            model_name='invoice',
            old_name='payment_method_new',
            new_name='payment_method',
        ),
        migrations.RemoveField(
            model_name='purchaseinvoice',
            name='payment_account',
        ),
        migrations.RenameField(
            model_name='purchaseinvoice',
            old_name='payment_method_new',
            new_name='payment_method',
        ),
        migrations.AlterField(
            model_name='purchaseinvoice',
            name='payment_method',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='purchase_invoices',
                to='managementsys.paymentmethod',
            ),
        ),
    ]
