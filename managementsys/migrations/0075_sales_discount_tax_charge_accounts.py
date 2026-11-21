from django.db import migrations


ACCOUNTS = [
    (4100000, 'Diskon Penjualan (Sales Discount)', 'revenue', 4000000),
    (2200000, 'Utang Pajak (Tax Payable)', 'liability', 2000000),
    (7100000, 'Pendapatan Lain-lain (Biaya Tambahan)', 'other_income', 7000000),
]


def create_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    for number, name, acct_type, head in ACCOUNTS:
        if ChartOfAccounts.objects.filter(account_number=number).exists():
            continue
        ChartOfAccounts.objects.create(
            account_number=number,
            name=name,
            account_type=acct_type,
            balance=0,
            is_system=True,
            is_head=False,
            parent=ChartOfAccounts.objects.filter(account_number=head, is_head=True).first(),
        )


def delete_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    numbers = [n for n, _nm, _t, _h in ACCOUNTS]
    used = set(
        LedgerEntry.objects
        .filter(account__account_number__in=numbers)
        .values_list('account__account_number', flat=True)
    )
    ChartOfAccounts.objects.filter(
        account_number__in=[n for n in numbers if n not in used]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0074_purchase_batch_value_backfill'),
    ]

    operations = [
        migrations.RunPython(create_accounts, delete_accounts),
    ]
