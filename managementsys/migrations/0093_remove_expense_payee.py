from django.db import migrations


class Migration(migrations.Migration):
    """Drop Expense.payee.

    The field was the expense's human-readable identifier (list column, detail
    heading, journal memo fallback, ledger PDF reference). Everything that used
    it now falls back to the payment memo, the first line's account, or the
    expense pk — see ``services.journal_engine.expense_leg_memo`` and
    ``services.ledger_pdf``. Existing journal rows keep whatever memo string
    they were written with; only future postings change wording.
    """

    dependencies = [
        ('managementsys', '0092_backfill_expense_payment_account'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='expense',
            name='payee',
        ),
    ]
