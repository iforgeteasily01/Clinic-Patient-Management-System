"""Backfill Expense.payment_account from the payment method it used to hide behind.

Every expense that named a payment method but has no explicit cash/bank
account yet gets ``payment_account_id = payment_method.linked_account_id``, so
the redesigned expense page shows a real account for historical rows instead
of an empty card.

``LedgerEntry`` is deliberately left alone — historical postings stay exactly
as they were written. This only fills in the column the UI reads.

The reverse clears ``payment_account`` on precisely the rows this migration
could have set (those whose account still matches their payment method's
linked account), leaving any row a user has since pointed somewhere else
untouched.
"""
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
