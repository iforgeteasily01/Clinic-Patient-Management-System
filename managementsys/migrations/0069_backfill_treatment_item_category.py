from django.db import migrations
from django.db.models import Max


REVENUE_RANGE = (4400000, 4999999)
COGS_RANGE = (5400000, 5999999)
EXPENSE_RANGE = (6900000, 6999999)


def _next_account_number(COA, range_min, range_max, step=1000):
    max_num = (
        COA.objects
        .filter(account_number__gte=range_min, account_number__lte=range_max)
        .aggregate(m=Max('account_number'))['m']
    )
    nxt = (max_num + step) if max_num is not None else range_min
    if nxt > range_max:
        raise ValueError(f'Account range {range_min}-{range_max} is exhausted.')
    return nxt


def _ensure_accounts(cat, COA):
    changed = False
    if cat.revenue_account_id is None:
        cat.revenue_account = COA.objects.create(
            account_number=_next_account_number(COA, *REVENUE_RANGE),
            name=f'Treatment Revenue – {cat.name}', account_type='revenue',
        )
        changed = True
    if hasattr(cat, 'cogs_account_id') and cat.cogs_account_id is None:
        cat.cogs_account = COA.objects.create(
            account_number=_next_account_number(COA, *COGS_RANGE),
            name=f'COGS – {cat.name}', account_type='cogs',
        )
        changed = True
    if hasattr(cat, 'expense_account_id') and cat.expense_account_id is None:
        cat.expense_account = COA.objects.create(
            account_number=_next_account_number(COA, *EXPENSE_RANGE),
            name=f'Expense – {cat.name}', account_type='expense',
        )
        changed = True
    if changed:
        cat.save()


def backfill(apps, schema_editor):
    Treatment = apps.get_model('managementsys', 'Treatment')
    TreatmentCategory = apps.get_model('managementsys', 'TreatmentCategory')
    COA = apps.get_model('managementsys', 'ChartOfAccounts')

    cats = {c.name.strip().lower(): c for c in TreatmentCategory.objects.all()}

    for treatment in Treatment.objects.select_related('catalog_item').all():
        if not treatment.catalog_item_id:
            continue
        name = (treatment.category or '').strip()
        if not name:
            continue
        key = name.lower()
        cat = cats.get(key)
        if cat is None:
            cat = TreatmentCategory.objects.create(name=name)
            cats[key] = cat
        _ensure_accounts(cat, COA)

        item = treatment.catalog_item
        if item.item_category_id != cat.id:
            item.item_category_id = cat.id
            item.save(update_fields=['item_category'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0068_appointmentlocation_seed'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
