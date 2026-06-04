from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0047_so_template'),
    ]

    operations = [
        migrations.CreateModel(
            name='LedgerEntry',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('date', models.DateField()),
                ('description', models.CharField(max_length=255)),
                ('entry_type', models.CharField(choices=[('debit', 'Debit'), ('credit', 'Credit')], max_length=6)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=18)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('account', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='ledger_entries',
                    to='managementsys.chartofaccounts',
                )),
                ('invoice', models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='ledger_entries',
                    to='managementsys.invoice',
                )),
            ],
            options={
                'verbose_name_plural': 'Ledger Entries',
                'ordering': ['-date', '-created_at'],
            },
        ),
    ]
