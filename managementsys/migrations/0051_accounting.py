from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0050_merge_20260605_0111'),
    ]

    operations = [
        # ── Supplier ───────────────────────────────────────────────────────────
        migrations.CreateModel(
            name='Supplier',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('contact_name', models.CharField(blank=True, max_length=100)),
                ('phone', models.CharField(blank=True, max_length=50)),
                ('email', models.EmailField(blank=True)),
                ('address', models.TextField(blank=True)),
                ('is_active', models.BooleanField(default=True)),
            ],
            options={'ordering': ['name']},
        ),

        # ── PurchaseInvoice ────────────────────────────────────────────────────
        migrations.CreateModel(
            name='PurchaseInvoice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('internal_id', models.CharField(blank=True, max_length=30, unique=True)),
                ('external_invoice_no', models.CharField(blank=True, max_length=100)),
                ('purchase_date', models.DateField()),
                ('due_date', models.DateField(blank=True, null=True)),
                ('status', models.CharField(
                    choices=[('unpaid', 'Belum Dibayar'), ('partial', 'Sebagian Dibayar'), ('paid', 'Lunas')],
                    default='unpaid', max_length=10,
                )),
                ('notes', models.TextField(blank=True)),
                ('total_amount', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('amount_paid', models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='purchase_invoices',
                    to='managementsys.appuser',
                )),
                ('payment_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='purchase_invoices',
                    to='managementsys.chartofaccounts',
                    limit_choices_to={'account_type': 'asset'},
                )),
                ('supplier', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='purchase_invoices',
                    to='managementsys.supplier',
                )),
            ],
            options={'ordering': ['-purchase_date', '-created_at']},
        ),

        # ── PurchaseInvoiceItem ────────────────────────────────────────────────
        migrations.CreateModel(
            name='PurchaseInvoiceItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_name', models.CharField(max_length=255)),
                ('quantity', models.DecimalField(decimal_places=3, max_digits=14)),
                ('unit', models.CharField(blank=True, max_length=50)),
                ('unit_cost', models.DecimalField(decimal_places=2, max_digits=14)),
                ('invoice', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='items',
                    to='managementsys.purchaseinvoice',
                )),
                ('item', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='purchase_items',
                    to='managementsys.inventoryitem',
                )),
            ],
            options={'ordering': ['id']},
        ),

        # ── AccountTransfer ────────────────────────────────────────────────────
        migrations.CreateModel(
            name='AccountTransfer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('transfer_date', models.DateField()),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('description', models.CharField(max_length=255)),
                ('reference', models.CharField(blank=True, max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(
                    blank=True, null=True,
                    on_delete=django.db.models.deletion.SET_NULL,
                    related_name='account_transfers',
                    to='managementsys.appuser',
                )),
                ('from_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='transfers_out',
                    to='managementsys.chartofaccounts',
                )),
                ('to_account', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='transfers_in',
                    to='managementsys.chartofaccounts',
                )),
            ],
            options={'ordering': ['-transfer_date', '-created_at']},
        ),

        # ── Extend LedgerEntry ─────────────────────────────────────────────────
        migrations.AddField(
            model_name='ledgerentry',
            name='source_type',
            field=models.CharField(
                blank=True, default='', max_length=15,
                choices=[
                    ('invoice',    'Sales Invoice'),
                    ('purchase',   'Purchase Invoice'),
                    ('transfer',   'Account Transfer'),
                    ('adjustment', 'Manual Adjustment'),
                    ('stock',      'Stock Movement'),
                    ('opname',     'Stock Opname'),
                    ('manual',     'Manual Entry'),
                ],
            ),
        ),
        migrations.AddField(
            model_name='ledgerentry',
            name='purchase_invoice',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='ledger_entries',
                to='managementsys.purchaseinvoice',
            ),
        ),
        migrations.AddField(
            model_name='ledgerentry',
            name='transfer',
            field=models.ForeignKey(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='ledger_entries',
                to='managementsys.accounttransfer',
            ),
        ),
    ]
