import django.db.models.deletion
from django.db import migrations, models


def create_catalog_items(apps, schema_editor):
    Treatment = apps.get_model('managementsys', 'Treatment')
    InventoryItem = apps.get_model('managementsys', 'InventoryItem')
    for treatment in Treatment.objects.filter(catalog_item__isnull=True):
        item = InventoryItem.objects.create(
            code=treatment.code,
            name=treatment.name,
            selling_price=treatment.price,
            unit_small='session',
            is_service=True,
            is_active=treatment.active,
            min_stock=0,
        )
        Treatment.objects.filter(pk=treatment.pk).update(catalog_item=item)


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0019_treatmentcategory'),
    ]

    operations = [
        migrations.AddField(
            model_name='inventoryitem',
            name='is_service',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='treatment',
            name='catalog_item',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='treatment',
                to='managementsys.inventoryitem',
            ),
        ),
        migrations.RunPython(create_catalog_items, migrations.RunPython.noop),
    ]
