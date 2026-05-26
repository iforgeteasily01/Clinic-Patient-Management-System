from django.db import migrations, models
import django.db.models.deletion


def seed_base_cogs_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    # Ensure the two base COGS system accounts exist (idempotent)
    for account_number, name in [
        (5100000, 'Cost of Products Sold'),
        (5200000, 'Treatment Supplies Consumed'),
    ]:
        ChartOfAccounts.objects.get_or_create(
            account_number=account_number,
            defaults={'name': name, 'account_type': 'cogs', 'balance': 0, 'is_system': True},
        )


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0030_appuser_theme_background_appuser_theme_primary_and_more'),
    ]

    operations = [
        # Add cogs_account FK to TreatmentCategory
        migrations.AddField(
            model_name='treatmentcategory',
            name='cogs_account',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='cogs_category',
                to='managementsys.chartofaccounts',
            ),
        ),
        # Add item_category FK to InventoryItem
        migrations.AddField(
            model_name='inventoryitem',
            name='item_category',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='inventory_items',
                to='managementsys.treatmentcategory',
            ),
        ),
        # Ensure base COGS system accounts exist
        migrations.RunPython(seed_base_cogs_accounts, migrations.RunPython.noop),
    ]
