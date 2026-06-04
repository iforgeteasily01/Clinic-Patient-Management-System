from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0045_stockopnamesession_warehouse'),
        ('managementsys', '0046_treatmentcategory_expense_account'),
    ]

    operations = [
        migrations.CreateModel(
            name='StockOpnameTemplate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('warehouse', models.OneToOneField(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='so_template',
                    to='managementsys.warehouse',
                )),
                ('created_by', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='so_templates_created',
                    to='managementsys.appuser',
                )),
            ],
        ),
        migrations.CreateModel(
            name='StockOpnameTemplateItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('section', models.CharField(max_length=200)),
                ('section_order', models.PositiveIntegerField(default=0)),
                ('item_order', models.PositiveIntegerField(default=0)),
                ('item', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    to='managementsys.inventoryitem',
                )),
                ('template', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='template_items',
                    to='managementsys.stockopnametemplate',
                )),
            ],
            options={
                'ordering': ['section_order', 'item_order'],
                'unique_together': {('template', 'item')},
            },
        ),
    ]
