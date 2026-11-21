from django.db import migrations

TYPE_HEAD_NUMBER = {
    'asset':         1000000,
    'liability':     2000000,
    'equity':        3000000,
    'revenue':       4000000,
    'cogs':          5000000,
    'expense':       6000000,
    'other_income':  7000000,
    'other_expense': 8000000,
}


def forward(apps, schema_editor):
    COA = apps.get_model('managementsys', 'ChartOfAccounts')

    heads = {
        acc.account_type: acc
        for acc in COA.objects.filter(
            account_number__in=TYPE_HEAD_NUMBER.values(), is_head=True
        )
    }

    for acc in COA.objects.filter(is_head=False, parent__isnull=True):
        head = heads.get(acc.account_type)
        if head is None or head.pk == acc.pk:
            continue
        COA.objects.filter(pk=acc.pk).update(parent=head)


def backward(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0078_fix_ipos_invoice_timezone'),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=backward),
    ]
