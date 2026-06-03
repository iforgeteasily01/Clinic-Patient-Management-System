from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0043_add_ticket_category'),
    ]

    operations = [
        migrations.CreateModel(
            name='StockOutLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('out_date', models.DateField()),
                ('quantity', models.PositiveIntegerField()),
                ('notes', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('item', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='stock_out_logs',
                    to='managementsys.inventoryitem',
                )),
                ('warehouse', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='stock_out_logs',
                    to='managementsys.warehouse',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='stock_out_logs',
                    to='managementsys.appuser',
                )),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
