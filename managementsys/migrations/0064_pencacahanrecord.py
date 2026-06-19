from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0063_medrec_clinician'),
    ]

    operations = [
        migrations.CreateModel(
            name='PencacahanRecord',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('pencacahan_no', models.CharField(max_length=50, unique=True)),
                ('date', models.DateField()),
                ('source_quantity', models.DecimalField(decimal_places=4, max_digits=14)),
                ('target_quantity', models.DecimalField(decimal_places=4, max_digits=14)),
                ('value_transferred', models.DecimalField(decimal_places=2, max_digits=14)),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='pencacahan_records',
                    to='managementsys.appuser',
                )),
                ('source_item', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='pencacahan_source_records',
                    to='managementsys.inventoryitem',
                )),
                ('source_warehouse', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='pencacahan_source_records',
                    to='managementsys.warehouse',
                )),
                ('target_item', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='pencacahan_target_records',
                    to='managementsys.inventoryitem',
                )),
                ('target_warehouse', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='pencacahan_target_records',
                    to='managementsys.warehouse',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
