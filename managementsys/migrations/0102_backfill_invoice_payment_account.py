"""Backfill Invoice.payment_account from the payment method it used to hide behind.

Every invoice that named a payment method gets
``payment_account_id = payment_method.linked_account_id``, mirroring what 0092 already
did for ``Expense``. Without this, every invoice posted before this shipment would show
no bank/cash account on the redesigned billing and invoice-detail pages, and
``journal_engine``'s new resolution order (payment_account, then payment_method.linked_account,
then the legacy clearing account) would fall through to the fallback for rows that already
had a perfectly good answer.

Grouped by distinct linked account rather than looped per invoice — this table has a lot of
rows, and there are only a handful of payment methods, so one ``.update()`` per method is a
constant number of queries regardless of invoice count.

Reverse is a no-op: leaving payment_account populated on rollback is harmless because nothing
reads it before this migration's forward run ships, and clearing it would just make the next
forward run redo identical work.
"""
from django.db import migrations


def backfill_payment_account(apps, schema_editor):
    Invoice = apps.get_model('managementsys', 'Invoice')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')

    linked_by_method = dict(
        PaymentMethod.objects.values_list('id', 'linked_account_id')
    )

    for method_id, account_id in linked_by_method.items():
        if not account_id:
            continue
        Invoice.objects.filter(
            payment_method_id=method_id,
            payment_account_id__isnull=True,
        ).update(payment_account_id=account_id)


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0101_report_settings_invoice_payment_account_expense_alias'),
    ]

    operations = [
        migrations.RunPython(backfill_payment_account, migrations.RunPython.noop),
    ]
