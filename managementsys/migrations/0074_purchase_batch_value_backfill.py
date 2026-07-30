"""Fix InventoryBatch.value for purchase-created batches + seed the price
variance account.

`value` is the TOTAL batch value everywhere in the system — the per-unit cost is
always `value / quantity_initial` (see _fifo_deduct/_fifo_restock, production
unit_cost, the test factory, and the stock-in "Total Value" field). The purchase
POST/PUT stored the *per-unit* cost instead, understating COGS on resale by a
factor of 1/qty. The code is fixed to store the total; this migration scales the
existing purchase-created batches (identified by a non-null purchase_invoice) up
to the total. Conversion/stock-in batches already store the total and are left
alone. Already-posted COGS ledger entries on past sales are historical and not
retroactively corrected.

It also creates the 5200000 "Selisih Harga Pembelian" (Purchase Price Variance)
account under the COGS head, used when a purchase invoice is edited after some of
its stock has already been sold.
"""
from django.db import migrations
from django.db.models import F

VARIANCE_NUMBER  = 5200000
COGS_HEAD_NUMBER = 5000000


def forward(apps, schema_editor):
    COA   = apps.get_model('managementsys', 'ChartOfAccounts')
    Batch = apps.get_model('managementsys', 'InventoryBatch')

    # Scale per-unit → total for existing purchase-created batches.
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
