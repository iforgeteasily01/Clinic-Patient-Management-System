from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0064_pencacahanrecord'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventorybatch',
            name='purchase_invoice',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='inventory_batches',
                to='managementsys.purchaseinvoice',
            ),
        ),
    ]
