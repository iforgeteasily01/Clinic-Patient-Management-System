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
