"""Retire the Undeposited Funds clearing account from everyday use.

Migration 0076 created 1100011 for receipts whose payment method was never
captured. In practice almost nothing landed there from the clinic: 25,234 of its
25,237 ledger rows belong to ``IPOS-*`` invoices — the imported iPos sales
history, whose cash side had nowhere else to go. The name "Undeposited Funds"
therefore describes the wrong thing, and the account's synthetic PaymentMethod
was offered in the POS, billing and expense dropdowns next to Cash and QRIS,
where a cashier could pick it by accident.

Nothing in the ledger moves. This renames the account to say what it actually
holds and deactivates its payment method so it disappears from every picker
(each one queries ``?active_only=1``; the desktop POS mirrors ``is_active``).
``services.cash_accounts`` excludes the number outright, so it also stops being
offered as somewhere to pay an expense or a purchase from.

Going forward no new receipts can reach it: BillingCompleteView now requires a
payment method, which was the only path that ever created a method-less invoice.
"""
from django.db import migrations


ACCOUNT_NUMBER = 1100011
OLD_NAME = 'Penerimaan Belum Teridentifikasi (Undeposited Funds)'
NEW_NAME = 'Kas Penjualan iPos (histori)'


def retire(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')

    account = ChartOfAccounts.objects.filter(account_number=ACCOUNT_NUMBER).first()
    if account is None:
        return
    ChartOfAccounts.objects.filter(pk=account.pk).update(name=NEW_NAME)
    # There is normally exactly one, created by repair_accounting_balance, but
    # filter rather than get so a hand-made second method is retired too.
    PaymentMethod.objects.filter(linked_account=account).update(
        name=NEW_NAME, is_active=False,
    )


def unretire(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')

    account = ChartOfAccounts.objects.filter(account_number=ACCOUNT_NUMBER).first()
    if account is None:
        return
    ChartOfAccounts.objects.filter(pk=account.pk).update(name=OLD_NAME)
    PaymentMethod.objects.filter(linked_account=account).update(
        name=OLD_NAME, is_active=True,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0099_backfill_purchase_payment_account'),
    ]

    operations = [
        migrations.RunPython(retire, unretire),
    ]
