from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0054_patient_optional_fields'),
    ]

    operations = [
        # PurchaseInvoice: add invoice-level warehouse
        migrations.AddField(
            model_name='purchaseinvoice',
            name='warehouse',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='purchase_invoices',
                to='managementsys.warehouse',
            ),
        ),

        # PurchaseInvoiceItem: per-item discount and final actual unit cost
        migrations.AddField(
            model_name='purchaseinvoiceitem',
            name='total_discount',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=14),
        ),
        migrations.AddField(
            model_name='purchaseinvoiceitem',
            name='actual_unit_cost',
            field=models.DecimalField(decimal_places=4, default=0, max_digits=14),
        ),

        # New model: PurchaseAdditionalCost
        migrations.CreateModel(
            name='PurchaseAdditionalCost',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=255)),
                ('modifier', models.CharField(
                    choices=[('add', 'Tambah'), ('subtract', 'Kurang')],
                    default='add',
                    max_length=10,
                )),
                ('amount_type', models.CharField(
                    choices=[('cash', 'Nominal'), ('percent', 'Persen')],
                    default='cash',
                    max_length=10,
                )),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('sort_order', models.PositiveSmallIntegerField(default=0)),
                ('invoice', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='additional_costs',
                    to='managementsys.purchaseinvoice',
                )),
            ],
            options={
                'ordering': ['sort_order', 'id'],
            },
        ),
    ]
