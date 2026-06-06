from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0052_treatmentcategory_show_to_beautician'),
    ]

    operations = [
        migrations.AddField(
            model_name='purchaseinvoice',
            name='invoice_image',
            field=models.ImageField(blank=True, null=True, upload_to='purchase_invoices/'),
        ),
        migrations.AddField(
            model_name='purchaseinvoiceitem',
            name='line_type',
            field=models.CharField(
                choices=[('stock', 'Stok'), ('expense', 'Beban')],
                default='stock',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='purchaseinvoiceitem',
            name='expense_account',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='purchase_expense_items',
                to='managementsys.chartofaccounts',
            ),
        ),
        migrations.AddField(
            model_name='purchaseinvoiceitem',
            name='warehouse',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='purchase_items',
                to='managementsys.warehouse',
            ),
        ),
    ]
