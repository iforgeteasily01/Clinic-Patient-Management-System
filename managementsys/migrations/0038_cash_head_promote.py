from django.db import migrations

CASH_HEAD_NUMBER = 1100000


def forward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')

    cash_head = COA.objects.filter(account_number=CASH_HEAD_NUMBER).first()
    if not cash_head:
        return
    cash_head.is_head = True
    cash_head.is_system = True
    cash_head.parent = None
    cash_head.save()

    assets_head = COA.objects.filter(account_number=1000000).first()
    subs = COA.objects.filter(
        account_number__gte=1100000,
        account_number__lte=1199999,
        is_head=False,
    ).exclude(pk=cash_head.pk)
    subs.update(parent=cash_head)

    COA.objects.filter(
        account_number__gte=1100000,
        account_number__lte=1199999,
        is_head=False,
        parent__isnull=True,
    ).exclude(pk=cash_head.pk).update(parent=cash_head)

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
        if assets_head:
            COA.objects.filter(parent=cash_head).update(parent=assets_head)


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0037_coa_reseed_heads'),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=backward),
    ]
