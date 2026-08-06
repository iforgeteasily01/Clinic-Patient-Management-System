from django.db import migrations

# Head account number per account type (seeded by 0037_coa_reseed_heads).
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
    """File every parentless sub-account under its account type's head.

    Per-category revenue/COGS/expense accounts were created without a parent,
    so the Chart of Accounts page — which walks the tree from the heads down —
    never displayed them.
    """
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
    # Non-destructive forward migration; nothing to undo safely.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0078_fix_ipos_invoice_timezone'),
    ]

    operations = [
        migrations.RunPython(forward, reverse_code=backward),
    ]
