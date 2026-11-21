from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import F, Sum

from .models import ChartOfAccounts, InventoryBatch, Invoice, InvoiceItem, LedgerEntry
from .services.journal_engine import ACC_INVENTORY, DEBIT_NORMAL_TYPES

D = Decimal

GO_LIVE = date(2026, 6, 5)

IMPORTED_INVOICE_PREFIX = 'IPOS'

DOCUMENT_FIELDS = ('invoice', 'purchase_invoice', 'transfer')


def trial_balance():
    dr = LedgerEntry.objects.filter(entry_type='debit').aggregate(t=Sum('amount'))['t'] or D('0')
    cr = LedgerEntry.objects.filter(entry_type='credit').aggregate(t=Sum('amount'))['t'] or D('0')
    return dr, cr


def unbalanced_documents(field):
    grouped = defaultdict(lambda: [D('0'), D('0')])
    for row in (LedgerEntry.objects.exclude(**{f'{field}__isnull': True})
                .values(f'{field}_id', 'entry_type').annotate(t=Sum('amount'))):
        idx = 0 if row['entry_type'] == 'debit' else 1
        grouped[row[f'{field}_id']][idx] += row['t'] or D('0')
    return [pk for pk, (dr, cr) in grouped.items() if dr != cr]


def derived_balance(account, sums=None):
    if sums is None:
        dr = account.ledger_entries.filter(entry_type='debit').aggregate(t=Sum('amount'))['t'] or D('0')
        cr = account.ledger_entries.filter(entry_type='credit').aggregate(t=Sum('amount'))['t'] or D('0')
    else:
        dr, cr = sums
    return (dr - cr) if account.account_type in DEBIT_NORMAL_TYPES else (cr - dr)


def balance_drift():
    agg = defaultdict(lambda: [D('0'), D('0')])
    for row in (LedgerEntry.objects.values('account_id', 'entry_type')
                .annotate(total=Sum('amount'))):
        agg[row['account_id']][0 if row['entry_type'] == 'debit' else 1] = row['total'] or D('0')

    drift = []
    for account in ChartOfAccounts.objects.all().order_by('account_number'):
        derived = derived_balance(account, agg.get(account.id, [D('0'), D('0')]))
        if account.balance != derived:
            drift.append((account, account.balance, derived))
    return drift


def inventory_on_hand_value():
    total = D('0')
    for value, qi, qr in InventoryBatch.objects.values_list(
            'value', 'quantity_initial', 'quantity_remaining'):
        if not qi:
            continue
        total += (value * qr / qi)
    return total.quantize(D('0.01'))


def inventory_tie():
    account = ChartOfAccounts.objects.filter(account_number=ACC_INVENTORY).first()
    gl = derived_balance(account) if account else D('0')
    on_hand = inventory_on_hand_value()
    return gl, on_hand, on_hand - gl


def invoices_missing_from_ledger(cutoff=GO_LIVE, include_imported=False):
    posted = LedgerEntry.objects.filter(invoice__isnull=False).values('invoice_id')
    qs = (Invoice.objects
          .filter(datetime__date__gte=cutoff, is_voided=False)
          .exclude(id__in=posted)
          .annotate(line_gross=Sum(F('items__price') * F('items__quantity')))
          .exclude(grand_total=0, line_gross__isnull=True)
          .exclude(grand_total=0, line_gross=0))
    if not include_imported:
        qs = qs.exclude(invoice_number__startswith=IMPORTED_INVOICE_PREFIX)
    return qs


def non_reconciling_invoices(cutoff=GO_LIVE):
    lines = defaultdict(list)
    for item in InvoiceItem.objects.filter(
            invoice__datetime__date__gte=cutoff, invoice__is_voided=False
    ).exclude(
        invoice__invoice_number__startswith=IMPORTED_INVOICE_PREFIX
    ).values_list('invoice_id', 'price', 'quantity', 'discount_pct'):
        lines[item[0]].append(item[1:])

    out = []
    for invoice in Invoice.objects.filter(id__in=lines.keys()):
        subtotal = sum(
            (price * qty * (D('1') - pct / D('100')) for price, qty, pct in lines[invoice.id]),
            D('0'),
        )
        gap = (subtotal - invoice.discount + invoice.tax
               + invoice.additional_charges - invoice.grand_total)
        if gap:
            out.append((invoice, gap))
    return out
