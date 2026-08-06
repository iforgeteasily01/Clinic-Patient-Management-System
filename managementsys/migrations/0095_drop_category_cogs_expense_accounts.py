"""Delete the per-treatment-category COGS and Expense GL accounts.

Phase 3 (migrations 0085/0086) dropped ``TreatmentCategory.cogs_account`` and
``TreatmentCategory.expense_account`` but deliberately left the accounts
themselves in place, in case any carried ledger history. They never did: the
posting engine routes product cost to the shared 5100000 "Cost of Products
Sold" account, and operating expenses now go through the Expense/ExpenseItem
model, which picks its account per line. So the rows have sat in the Chart of
Accounts as ~60 permanently-empty entries named ``COGS – <category>`` and
``Expense – <category>``, cluttering the COGS and Expense heads.

Two safety rails:

* Only accounts with **no** ledger entries and a zero balance are removed, and
  system/head accounts are excluded outright. ``LedgerEntry.account`` is
  PROTECT, so a posted account would raise here rather than lose history —
  the filter skips it instead and the account simply stays.
* Names are matched on the exact ``COGS – ``/``Expense – `` prefixes (en dash)
  written by ``TreatmentCategory.ensure_accounts()`` and migration 0069, and
  are further constrained to the number ranges those helpers allocate from
  (5400000+ for COGS, 6900000+ for expense). A hand-keyed account that merely
  starts with the word "Expense" outside that range is left alone.

Freeing 6910000/6920000/6930000 also unblocks three rows from migration 0094:
the client's COA seeds "Biaya Penyusutan Gedung / Kendaraan / Peralatan" at
those numbers, but 0094 skips any number already taken, and the category
Expense accounts had claimed all three. They are created here once the
squatters are gone.
"""
from django.db import migrations
from django.db.models import Q

# Ranges TreatmentCategory.ensure_accounts() / migration 0069 allocate from.
COGS_RANGE = (5400000, 5999999)
EXPENSE_RANGE = (6900000, 6999999)

# Depreciation accounts from 0094 that collided with the category accounts.
DEPRECIATION_SEEDS = [
    (6910000, 'Biaya Penyusutan Gedung'),
    (6920000, 'Biaya Penyusutan Kendaraan'),
    (6930000, 'Biaya Penyusutan Peralatan'),
]


def orphan_qs(COA):
    return (
        COA.objects
        .filter(is_system=False, is_head=False, balance=0)
        .filter(
            Q(name__startswith='COGS – ',
              account_type='cogs',
              account_number__gte=COGS_RANGE[0], account_number__lte=COGS_RANGE[1])
            | Q(name__startswith='Expense – ',
                account_type='expense',
                account_number__gte=EXPENSE_RANGE[0], account_number__lte=EXPENSE_RANGE[1])
        )
        .filter(ledger_entries__isnull=True)
    )


def forward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')

    # .delete() on a sliced/filtered-across-join queryset needs the ids first.
    ids = list(orphan_qs(COA).values_list('id', flat=True))
    COA.objects.filter(id__in=ids).delete()

    expense_head = COA.objects.filter(account_number=6000000, is_head=True).first()
    if expense_head is None:
        return
    for number, name in DEPRECIATION_SEEDS:
        COA.objects.get_or_create(
            account_number=number,
            defaults={
                'name': name,
                'account_type': 'expense',
                'balance': 0,
                'is_system': False,
                'is_head': False,
                'parent': expense_head,
            },
        )


def backward(apps, schema_editor):
    # Irreversible: the categories these accounts belonged to are no longer
    # recorded anywhere, so there is nothing to recreate them from.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0094_coa_seed_cogs_expense_other'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
