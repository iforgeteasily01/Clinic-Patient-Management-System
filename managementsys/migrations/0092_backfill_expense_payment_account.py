from django.db import migrations


def backfill_payment_account(apps, schema_editor):
    Expense = apps.get_model('managementsys', 'Expense')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')

    linked_by_method = dict(
        PaymentMethod.objects.values_list('id', 'linked_account_id')
    )

    for method_id, account_id in linked_by_method.items():
        if not account_id:
            continue
        Expense.objects.filter(
            payment_method_id=method_id,
            payment_account_id__isnull=True,
        ).update(payment_account_id=account_id)


def clear_backfilled_payment_account(apps, schema_editor):
    Expense = apps.get_model('managementsys', 'Expense')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')

    linked_by_method = dict(
        PaymentMethod.objects.values_list('id', 'linked_account_id')
    )

    for method_id, account_id in linked_by_method.items():
        if not account_id:
            continue
        Expense.objects.filter(
            payment_method_id=method_id,
            payment_account_id=account_id,
        ).update(payment_account_id=None)


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0091_expense_payment_account'),
    ]

    operations = [
        migrations.RunPython(backfill_payment_account, clear_backfilled_payment_account),
    ]
