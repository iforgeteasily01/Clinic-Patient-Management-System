from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0031_category_cogs_item_category'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoice',
            name='notes',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
        migrations.AddField(
            model_name='invoice',
            name='promotion_code',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
        migrations.AddField(
            model_name='invoiceitem',
            name='item_name',
            field=models.CharField(blank=True, default='', max_length=255),
        ),
        migrations.AddField(
            model_name='invoiceitem',
            name='discount_pct',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=5),
        ),
        migrations.AlterField(
            model_name='invoiceitem',
            name='item',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name='invoice_items',
                to='managementsys.inventoryitem',
            ),
        ),
    ]
