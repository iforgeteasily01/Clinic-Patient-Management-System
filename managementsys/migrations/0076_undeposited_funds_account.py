from django.db import migrations


ACCOUNT_NUMBER = 1100011
ACCOUNT_NAME = 'Penerimaan Belum Teridentifikasi (Undeposited Funds)'


def create_account(apps, schema_editor):
    """Clearing account for receipts whose payment method was never captured.

    The billing queue completed invoices without recording how the patient paid.
    Those receipts are real, so they must reach the books, but posting them to
    Cash would assert a payment method that was never known. They land here
    instead, where a bookkeeper can reallocate them to the real account later.
    """
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    if ChartOfAccounts.objects.filter(account_number=ACCOUNT_NUMBER).exists():
        return
    ChartOfAccounts.objects.create(
        account_number=ACCOUNT_NUMBER,
        name=ACCOUNT_NAME,
        account_type='asset',
        balance=0,
        is_system=True,
        is_head=False,
        parent=ChartOfAccounts.objects.filter(account_number=1100000, is_head=True).first(),
    )


def delete_account(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    # LedgerEntry.account is PROTECT; leave the account alone once it has postings.
    if LedgerEntry.objects.filter(account__account_number=ACCOUNT_NUMBER).exists():
        return
    ChartOfAccounts.objects.filter(account_number=ACCOUNT_NUMBER).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0075_sales_discount_tax_charge_accounts'),
    ]

    operations = [
        migrations.RunPython(create_account, delete_account),
    ]
