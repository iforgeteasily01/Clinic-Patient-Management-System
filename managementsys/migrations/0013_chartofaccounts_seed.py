from django.db import migrations

SEED_ACCOUNTS = [
    # ── Assets (1xxxxxx) ──────────────────────────────────────────────────
    (1100000, 'Cash on Hand',                       'asset'),
    (1110000, 'Cash in Bank',                       'asset'),
    (1200000, 'Accounts Receivable',                'asset'),
    (1300000, 'Inventory – Products',               'asset'),
    (1310000, 'Inventory – Treatment Supplies',     'asset'),
    (1400000, 'Prepaid Expenses',                   'asset'),
    (1500000, 'Equipment',                          'asset'),
    (1510000, 'Accumulated Depreciation',           'asset'),

    # ── Liabilities (2xxxxxx) ─────────────────────────────────────────────
    (2100000, 'Accounts Payable',                   'liability'),
    (2200000, 'Accrued Liabilities',                'liability'),
    (2300000, 'VAT / Tax Payable',                  'liability'),
    (2400000, 'Short-term Loans Payable',           'liability'),
    (2500000, 'Long-term Loans Payable',            'liability'),

    # ── Equity (3xxxxxx) ──────────────────────────────────────────────────
    (3100000, "Owner's Capital",                    'equity'),
    (3200000, 'Retained Earnings',                  'equity'),
    (3300000, 'Current Period Earnings',            'equity'),

    # ── Revenue (4xxxxxx) ─────────────────────────────────────────────────
    (4100000, 'Treatment Revenue',                  'revenue'),
    (4200000, 'Product Sales Revenue',              'revenue'),
    (4300000, 'Consultation Revenue',               'revenue'),

    # ── Cost of Goods Sold (5xxxxxx) ──────────────────────────────────────
    (5100000, 'Cost of Products Sold',              'cogs'),
    (5200000, 'Treatment Supplies Consumed',        'cogs'),

    # ── Operating Expenses (6xxxxxx) ──────────────────────────────────────
    (6100000, 'Salaries & Wages',                   'expense'),
    (6200000, 'Rent Expense',                       'expense'),
    (6300000, 'Utilities Expense',                  'expense'),
    (6400000, 'Marketing & Advertising',            'expense'),
    (6500000, 'Equipment Maintenance',              'expense'),
    (6600000, 'Depreciation Expense',               'expense'),
    (6700000, 'Insurance Expense',                  'expense'),
    (6800000, 'Office Supplies Expense',            'expense'),

    # ── Other Income (7xxxxxx) ────────────────────────────────────────────
    (7100000, 'Interest Income',                    'other_income'),
    (7200000, 'Miscellaneous Income',               'other_income'),

    # ── Other Expenses (8xxxxxx) ──────────────────────────────────────────
    (8100000, 'Interest Expense',                   'other_expense'),
    (8200000, 'Bank Charges',                       'other_expense'),
    (8300000, 'Miscellaneous Expense',              'other_expense'),
]


def seed_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    ChartOfAccounts.objects.bulk_create([
        ChartOfAccounts(
            account_number=acc_no,
            name=name,
            account_type=acc_type,
            balance=0,
            is_system=True,
        )
        for acc_no, name, acc_type in SEED_ACCOUNTS
    ])


def unseed_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    ChartOfAccounts.objects.filter(is_system=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0012_chartofaccounts'),
    ]

    operations = [
        migrations.RunPython(seed_accounts, reverse_code=unseed_accounts),
    ]
