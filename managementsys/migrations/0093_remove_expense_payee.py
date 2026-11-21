from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0092_backfill_expense_payment_account'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='expense',
            name='payee',
        ),
    ]
