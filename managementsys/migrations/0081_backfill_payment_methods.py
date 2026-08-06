"""Backfill PaymentMethod rows from the ChartOfAccounts rows they replace.

For every distinct ChartOfAccounts row referenced by Invoice.payment_method
(old FK) or PurchaseInvoice.payment_account, create one PaymentMethod whose
``linked_account`` is that account, named after it. Every Invoice / PurchaseInvoice
row is then repointed at the new PaymentMethod via the temporary
``payment_method_new`` column added in 0080. 0082 drops the old columns and
renames the new ones into their final place.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')
    Invoice = apps.get_model('managementsys', 'Invoice')
    PurchaseInvoice = apps.get_model('managementsys', 'PurchaseInvoice')

    account_ids = set(
        Invoice.objects.filter(payment_method__isnull=False)
        .values_list('payment_method_id', flat=True).distinct()
    ) | set(
        PurchaseInvoice.objects.values_list('payment_account_id', flat=True).distinct()
    )
    account_ids.discard(None)

    accounts = {a.id: a for a in ChartOfAccounts.objects.filter(id__in=account_ids)}

    method_by_account_id = {}
    for account_id, account in accounts.items():
        method, _ = PaymentMethod.objects.get_or_create(
            linked_account_id=account_id,
            defaults={
                'name': account.name,
                'is_active': True,
                'is_system': account.is_system,
            },
        )
        method_by_account_id[account_id] = method.id

    for account_id, method_id in method_by_account_id.items():
        Invoice.objects.filter(payment_method_id=account_id).update(
            payment_method_new_id=method_id
        )
        PurchaseInvoice.objects.filter(payment_account_id=account_id).update(
            payment_method_new_id=method_id
        )


def noop(apps, schema_editor):
    # Data backfill only; the columns it populated are dropped by 0082, and
    # 0080's reverse already removes payment_method_new, so there is nothing
    # left to undo here.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0080_paymentmethod'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
