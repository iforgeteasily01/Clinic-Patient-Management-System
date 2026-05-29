from django.db import migrations

# ── Head accounts: one per account type ──────────────────────────────────────
# is_system=True only on the 4 accounts the code references by number.
HEAD_ACCOUNTS = [
    # (account_number, name, account_type, is_system)
    (1000000, 'Assets',             'asset',         False),
    (2000000, 'Liabilities',        'liability',     False),
    (3000000, 'Equity',             'equity',        False),
    (4000000, 'Revenue',            'revenue',       False),
    (5000000, 'Cost of Goods Sold', 'cogs',          False),
    (6000000, 'Expenses',           'expense',       False),
    (7000000, 'Other Income',       'other_income',  False),
    (8000000, 'Other Expenses',     'other_expense', False),
]

# ── Sub-accounts the system uses by number ────────────────────────────────────
SYSTEM_SUB_ACCOUNTS = [
    # (account_number, name, account_type, parent_number)
    (1100000, 'Cash & Payment Accounts', 'asset',   1000000),
    (1300000, 'Inventory – Products',    'asset',   1000000),
    (4200000, 'Product Sales Revenue',   'revenue', 4000000),
    (5100000, 'Cost of Products Sold',   'cogs',    5000000),
]

# All old seed account numbers to remove
OLD_SEED_NUMBERS = {
    1100000, 1110000, 1200000, 1300000, 1310000, 1400000, 1500000, 1510000,
    2100000, 2200000, 2300000, 2400000, 2500000,
    3100000, 3200000, 3300000,
    4100000, 4200000, 4300000,
    5100000, 5200000,
    6100000, 6200000, 6300000, 6400000, 6500000, 6600000, 6700000, 6800000,
    7100000, 7200000,
    8100000, 8200000, 8300000,
}


def forward(apps, schema_editor):
    COA     = apps.get_model('managementsys', 'ChartOfAccounts')
    Invoice = apps.get_model('managementsys', 'Invoice')

    # 1. Collect invoice IDs that point at an old seed cash account,
    #    then null the FK so PROTECT doesn't block deletion.
    affected_invoice_ids = list(
        Invoice.objects.filter(
            payment_method__account_number__in=OLD_SEED_NUMBERS
        ).values_list('id', flat=True)
    )
    if affected_invoice_ids:
        Invoice.objects.filter(id__in=affected_invoice_ids).update(payment_method=None)

    # 2. Clear parent FKs on old COA rows then delete them all.
    COA.objects.filter(account_number__in=OLD_SEED_NUMBERS).update(parent=None)
    COA.objects.filter(account_number__in=OLD_SEED_NUMBERS).delete()

    # 3. Create the 8 head accounts.
    heads = {}
    for acc_no, name, acc_type, is_sys in HEAD_ACCOUNTS:
        obj, _ = COA.objects.get_or_create(
            account_number=acc_no,
            defaults={
                'name': name,
                'account_type': acc_type,
                'balance': 0,
                'is_system': is_sys,
                'is_head': True,
                'parent': None,
            },
        )
        heads[acc_no] = obj

    # 4. Create the 4 system sub-accounts.
    for acc_no, name, acc_type, parent_no in SYSTEM_SUB_ACCOUNTS:
        COA.objects.update_or_create(
            account_number=acc_no,
            defaults={
                'name': name,
                'account_type': acc_type,
                'balance': 0,
                'is_system': True,
                'is_head': False,
                'parent': heads[parent_no],
            },
        )

    # 5. Re-point the affected invoices to the new cash sub-account.
    if affected_invoice_ids:
        new_cash = COA.objects.filter(account_number=1100000).first()
        if new_cash:
            Invoice.objects.filter(id__in=affected_invoice_ids).update(payment_method=new_cash)


def backward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')
    all_nos = {r[0] for r in HEAD_ACCOUNTS} | {r[0] for r in SYSTEM_SUB_ACCOUNTS}
    COA.objects.filter(account_number__in=all_nos).update(parent=None)
    COA.objects.filter(account_number__in=all_nos).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0036_coa_head_parent'),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=backward),
    ]
