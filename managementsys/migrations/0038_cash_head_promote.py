from django.db import migrations

CASH_HEAD_NUMBER = 1100000


def forward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')

    # Promote 1100000 to a head account (parent=None, is_head=True, is_system=True).
    cash_head = COA.objects.filter(account_number=CASH_HEAD_NUMBER).first()
    if not cash_head:
        return
    cash_head.is_head = True
    cash_head.is_system = True
    cash_head.parent = None
    cash_head.save()

    # Re-parent any sub-accounts that are still pointing at the Assets head
    # (1000000) but belong in the cash group (account_number 1100000–1199999),
    # plus any that have no parent at all in that range.
    assets_head = COA.objects.filter(account_number=1000000).first()
    subs = COA.objects.filter(
        account_number__gte=1100000,
        account_number__lte=1199999,
        is_head=False,
    ).exclude(pk=cash_head.pk)
    subs.update(parent=cash_head)

    # Also catch accounts with no parent that fell in cash range
    COA.objects.filter(
        account_number__gte=1100000,
        account_number__lte=1199999,
        is_head=False,
        parent__isnull=True,
    ).exclude(pk=cash_head.pk).update(parent=cash_head)

    # Fix any completely orphaned accounts outside the cash range that
    # ended up with parent=None and is_head=False (shouldn't happen, but clean up).
    if assets_head:
        COA.objects.filter(
            is_head=False,
            parent__isnull=True,
            account_number__gte=1000000,
            account_number__lte=1099999,
        ).update(parent=assets_head)


def backward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')
    assets_head = COA.objects.filter(account_number=1000000).first()
    cash_head = COA.objects.filter(account_number=CASH_HEAD_NUMBER).first()
    if cash_head:
        cash_head.is_head = False
        cash_head.parent = assets_head
        cash_head.save()
        # Re-point former subs back to assets head
        if assets_head:
            COA.objects.filter(parent=cash_head).update(parent=assets_head)


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0037_coa_reseed_heads'),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=backward),
    ]
