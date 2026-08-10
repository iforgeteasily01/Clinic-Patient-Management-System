"""Backfill the new COA payment_account from the legacy PaymentMethod link.

Purchases used to name a PaymentMethod and reach its ``linked_account`` at
posting time. The account is now stored directly, so every existing purchase
invoice and payment gets the account its method resolved to. Rows whose method
is null (unpaid invoices) stay null — they have no payment account yet.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    PurchaseInvoice = apps.get_model('managementsys', 'PurchaseInvoice')
    PurchasePayment = apps.get_model('managementsys', 'PurchasePayment')

    for model in (PurchaseInvoice, PurchasePayment):
        for row in model.objects.filter(
            payment_account__isnull=True, payment_method__isnull=False
        ).select_related('payment_method'):
            linked_id = row.payment_method.linked_account_id
            if linked_id:
                model.objects.filter(pk=row.pk).update(payment_account_id=linked_id)


def backwards(apps, schema_editor):
    # payment_method was never cleared, so there is nothing to restore.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0098_purchase_payment_account'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
