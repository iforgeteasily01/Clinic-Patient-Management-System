from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0047_so_template'),
    ]

    operations = [
        migrations.CreateModel(
            name='TreatmentMaterial',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('quantity_small', models.DecimalField(decimal_places=4, max_digits=10)),
                ('treatment', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='materials',
                    to='managementsys.treatment',
                )),
                ('item', models.ForeignKey(
                    limit_choices_to={'is_service': False},
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='treatment_materials',
                    to='managementsys.inventoryitem',
                )),
            ],
            options={
                'ordering': ['id'],
                'unique_together': {('treatment', 'item')},
            },
        ),
    ]
