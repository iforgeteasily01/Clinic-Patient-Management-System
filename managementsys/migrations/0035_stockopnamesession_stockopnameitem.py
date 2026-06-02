from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0034_protect_treatment_catalog_item'),
    ]

    operations = [
        migrations.CreateModel(
            name='StockOpnameSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('conducted_by', models.CharField(max_length=200)),
                ('notes', models.TextField(blank=True, default='')),
                ('status', models.CharField(
                    choices=[('draft', 'Draft'), ('completed', 'Selesai')],
                    default='draft',
                    max_length=20,
                )),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    to='managementsys.appuser',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
        migrations.CreateModel(
            name='StockOpnameItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('shelf1_qty', models.PositiveIntegerField(default=0)),
                ('shelf2_qty', models.PositiveIntegerField(default=0)),
                ('system_qty', models.PositiveIntegerField(default=0)),
                ('is_loss', models.BooleanField(default=False)),
                ('session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='items',
                    to='managementsys.stockopnamesession',
                )),
                ('item', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to='managementsys.inventoryitem',
                )),
                ('warehouse', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to='managementsys.warehouse',
                )),
            ],
            options={
                'unique_together': {('session', 'item', 'warehouse')},
            },
        ),
    ]
