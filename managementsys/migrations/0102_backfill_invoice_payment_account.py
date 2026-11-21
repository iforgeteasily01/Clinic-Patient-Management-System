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
