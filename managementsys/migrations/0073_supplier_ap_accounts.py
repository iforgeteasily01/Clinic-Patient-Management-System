from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models

# Chart-of-accounts numbering for the per-vendor Accounts Payable structure.
LIABILITIES_HEAD_NUMBER = 2000000
AP_CONTROL_NUMBER       = 2100000
AP_VENDOR_LO            = 2100001
AP_VENDOR_HI            = 2199999
EQUITY_HEAD_NUMBER      = 3000000
OBE_NUMBER              = 3900000   # Opening Balance Equity (backfill offset)

ZERO = Decimal('0')


def _get_or_create_account(COA, number, name, account_type, is_head, is_system, parent):
    obj = COA.objects.filter(account_number=number).first()
    if obj:
        return obj
    return COA.objects.create(
        account_number=number,
        name=name,
        account_type=account_type,
        balance=ZERO,
        is_head=is_head,
        is_system=is_system,
        parent=parent,
    )


def _next_ap_number(COA):
    existing = (
        COA.objects
        .filter(account_number__gte=AP_VENDOR_LO, account_number__lte=AP_VENDOR_HI)
        .order_by('-account_number')
        .values_list('account_number', flat=True)
        .first()
    )
    nxt = (existing + 1) if existing is not None else AP_VENDOR_LO
    if nxt > AP_VENDOR_HI:
        raise ValueError('AP vendor account range exhausted.')
    return nxt


def forward(apps, schema_editor):
    COA             = apps.get_model('managementsys', 'ChartOfAccounts')
    Supplier        = apps.get_model('managementsys', 'Supplier')
    PurchaseInvoice = apps.get_model('managementsys', 'PurchaseInvoice')
    LedgerEntry     = apps.get_model('managementsys', 'LedgerEntry')

    # 1. Heads must exist (created by 0037); create defensively if absent.
    liab_head = _get_or_create_account(
        COA, LIABILITIES_HEAD_NUMBER, 'Liabilities', 'liability',
        is_head=True, is_system=False, parent=None,
    )
    equity_head = _get_or_create_account(
        COA, EQUITY_HEAD_NUMBER, 'Equity', 'equity',
        is_head=True, is_system=False, parent=None,
    )

    # 2. AP control account + Opening Balance Equity offset account.
    ap_control = _get_or_create_account(
        COA, AP_CONTROL_NUMBER, 'Utang Usaha (Accounts Payable)', 'liability',
        is_head=False, is_system=True, parent=liab_head,
    )
    obe = _get_or_create_account(
        COA, OBE_NUMBER, 'Ekuitas Saldo Awal (Opening Balance Equity)', 'equity',
        is_head=False, is_system=True, parent=equity_head,
    )

    # 3. One AP sub-account per existing supplier.
    for supplier in Supplier.objects.filter(ap_account__isnull=True).order_by('id'):
        acct = COA.objects.create(
            account_number=_next_ap_number(COA),
            name=f'Utang Usaha – {supplier.name}',
            account_type='liability',
            balance=ZERO,
            is_head=False,
            is_system=True,
            parent=ap_control,
        )
        supplier.ap_account = acct
        supplier.save(update_fields=['ap_account'])

    # 4. Backfill OUTSTANDING payables only: post Cr AP-vendor / Dr Opening
    #    Balance Equity for each unpaid/partial (non-voided) invoice. Settled
    #    and voided invoices are left untouched.
    obe_delta = ZERO
    entries = []
    outstanding = (
        PurchaseInvoice.objects
        .filter(is_voided=False, status__in=['unpaid', 'partial'])
        .select_related('supplier', 'supplier__ap_account')
    )
    for inv in outstanding:
        balance_due = (inv.total_amount or ZERO) - (inv.amount_paid or ZERO)
        if balance_due <= 0:
            continue
        ap_acct = inv.supplier.ap_account
        if ap_acct is None:
            continue

        # Cr AP-vendor → increases the liability (credit-normal).
        COA.objects.filter(pk=ap_acct.pk).update(balance=models.F('balance') + balance_due)
        entries.append(LedgerEntry(
            account=ap_acct,
            date=inv.purchase_date,
            description=f'Saldo awal utang {inv.internal_id} — {inv.supplier.name}',
            entry_type='credit',
            amount=balance_due,
            source_type='purchase',
            purchase_invoice=inv,
        ))

        # Dr Opening Balance Equity → the offsetting contra (credit-normal, so a
        # debit reduces equity).
        entries.append(LedgerEntry(
            account=obe,
            date=inv.purchase_date,
            description=f'Saldo awal utang {inv.internal_id} — {inv.supplier.name}',
            entry_type='debit',
            amount=balance_due,
            source_type='purchase',
            purchase_invoice=inv,
        ))
        obe_delta += balance_due

    if entries:
        LedgerEntry.objects.bulk_create(entries)
    if obe_delta:
        # debit on a credit-normal account decreases its natural balance
        COA.objects.filter(pk=obe.pk).update(balance=models.F('balance') - obe_delta)


def backward(apps, schema_editor):
    COA         = apps.get_model('managementsys', 'ChartOfAccounts')
    Supplier    = apps.get_model('managementsys', 'Supplier')
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')

    # Remove backfill journal entries (both AP saldo-awal and OBE contra).
    LedgerEntry.objects.filter(
        source_type='purchase',
        description__startswith='Saldo awal utang ',
    ).delete()

    # Unlink and delete per-vendor AP accounts, the control account, and OBE.
    Supplier.objects.update(ap_account=None)
    COA.objects.filter(
        account_number__gte=AP_VENDOR_LO, account_number__lte=AP_VENDOR_HI
    ).update(parent=None)
    COA.objects.filter(
        account_number__gte=AP_VENDOR_LO, account_number__lte=AP_VENDOR_HI
    ).delete()
    COA.objects.filter(account_number__in=[AP_CONTROL_NUMBER, OBE_NUMBER]).update(parent=None)
    COA.objects.filter(account_number__in=[AP_CONTROL_NUMBER, OBE_NUMBER]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0072_medrec_obat_per_slot_detail'),
    ]

    operations = [
        migrations.AddField(
            model_name='supplier',
            name='ap_account',
            field=models.OneToOneField(
                blank=True, null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='supplier_ap',
                to='managementsys.chartofaccounts',
            ),
        ),
        migrations.RunPython(forward, reverse_code=backward),
    ]
