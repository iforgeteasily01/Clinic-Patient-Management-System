from django.db import migrations
from django.db.models import Q

COGS_RANGE = (5400000, 5999999)
EXPENSE_RANGE = (6900000, 6999999)

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
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0094_coa_seed_cogs_expense_other'),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
