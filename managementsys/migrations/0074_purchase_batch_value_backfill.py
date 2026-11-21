from django.db import migrations
from django.db.models import F

VARIANCE_NUMBER  = 5200000
COGS_HEAD_NUMBER = 5000000


def forward(apps, schema_editor):
    COA   = apps.get_model('managementsys', 'ChartOfAccounts')
    Batch = apps.get_model('managementsys', 'InventoryBatch')

    Batch.objects.filter(purchase_invoice__isnull=False).update(
        value=F('value') * F('quantity_initial')
    )

    head = COA.objects.filter(account_number=COGS_HEAD_NUMBER, is_head=True).first()
    COA.objects.get_or_create(
        account_number=VARIANCE_NUMBER,
        defaults={
            'name': 'Selisih Harga Pembelian',
            'account_type': 'cogs',
            'is_system': True,
            'is_head': False,
            'parent': head,
        },
    )


def backward(apps, schema_editor):
    COA   = apps.get_model('managementsys', 'ChartOfAccounts')
    Batch = apps.get_model('managementsys', 'InventoryBatch')

    Batch.objects.filter(
        purchase_invoice__isnull=False, quantity_initial__gt=0
    ).update(value=F('value') / F('quantity_initial'))
    COA.objects.filter(account_number=VARIANCE_NUMBER).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0073_supplier_ap_accounts'),
    ]
    operations = [
        migrations.RunPython(forward, backward),
    ]
