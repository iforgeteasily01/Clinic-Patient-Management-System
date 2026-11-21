import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0090_purchase_payments'),
    ]

    operations = [
        migrations.AddField(
            model_name='expense',
            name='payment_account',
            field=models.ForeignKey(blank=True, help_text='Cash/bank account credited by this expense. Must be one of services.cash_accounts.cash_bank_account_ids().', limit_choices_to={'account_type': 'asset'}, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='expenses_paid_from', to='managementsys.chartofaccounts'),
        ),
        migrations.AddField(
            model_name='expense',
            name='payment_memo',
            field=models.CharField(blank=True, help_text='Journal memo for the cash/bank (credit) leg. Also the fallback memo for any expense line left without one.', max_length=255),
        ),
        migrations.AlterField(
            model_name='expense',
            name='payment_method',
            field=models.ForeignKey(blank=True, help_text='Legacy indirection kept for back-compat. New records set payment_account directly; this stays populated only so old rows and any UI that still reasons in payment-method terms keep working.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='expenses', to='managementsys.paymentmethod'),
        ),
        migrations.AlterField(
            model_name='expenseitem',
            name='description',
            field=models.CharField(blank=True, help_text="This leg's journal memo. Blank inherits Expense.payment_memo.", max_length=255),
        ),
    ]
