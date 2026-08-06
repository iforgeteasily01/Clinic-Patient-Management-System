"""Purchase payments become their own records.

The payment method moves off PurchaseInvoice (where it was mandatory even for
an unpaid credit purchase) and onto each settlement, which also carries the
date it was paid. Existing invoices with amount_paid > 0 are backfilled with a
single PurchasePayment row for what they had already paid, using the method
that was recorded on the invoice and its purchase date.
"""

import django.db.models.deletion
from django.db import migrations, models


def backfill_payments(apps, schema_editor):
    PurchaseInvoice = apps.get_model('managementsys', 'PurchaseInvoice')
    PurchasePayment = apps.get_model('managementsys', 'PurchasePayment')

    rows = []
    for inv in PurchaseInvoice.objects.filter(amount_paid__gt=0).exclude(payment_method__isnull=True):
        rows.append(PurchasePayment(
            invoice_id=inv.id,
            payment_date=inv.purchase_date,
            payment_method_id=inv.payment_method_id,
            amount=inv.amount_paid,
            notes='Backfill — pembayaran sebelum pencatatan per-transaksi',
        ))
    PurchasePayment.objects.bulk_create(rows, batch_size=500)


def unbackfill_payments(apps, schema_editor):
    PurchasePayment = apps.get_model('managementsys', 'PurchasePayment')
    PurchasePayment.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0089_unify_bank_accounts'),
    ]

    operations = [
        migrations.AlterField(
            model_name='paymentmethod',
            name='id',
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID'),
        ),
        migrations.AlterField(
            model_name='purchaseinvoice',
            name='payment_method',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='purchase_invoices', to='managementsys.paymentmethod'),
        ),
        migrations.CreateModel(
            name='PurchasePayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('payment_date', models.DateField()),
                ('amount', models.DecimalField(decimal_places=2, max_digits=14)),
                ('notes', models.CharField(blank=True, max_length=255)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='purchase_payments', to='managementsys.appuser')),
                ('invoice', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='payments', to='managementsys.purchaseinvoice')),
                ('payment_method', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='purchase_payments', to='managementsys.paymentmethod')),
            ],
            options={
                'ordering': ['payment_date', 'id'],
            },
        ),
        migrations.RunPython(backfill_payments, unbackfill_payments),
    ]
