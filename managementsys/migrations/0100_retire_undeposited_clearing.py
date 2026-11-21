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
