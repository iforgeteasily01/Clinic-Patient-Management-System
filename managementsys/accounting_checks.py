"""Read-only integrity checks over the general ledger.

Shared by ``check_accounting_health`` (reports) and ``repair_accounting_balance``
(verifies its own work), so the two can never disagree about what "balanced" means.

Every function here is side-effect free.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.db.models import F, Sum

from .models import ChartOfAccounts, InventoryBatch, Invoice, InvoiceItem, LedgerEntry
from .services.journal_engine import ACC_INVENTORY, DEBIT_NORMAL_TYPES

D = Decimal

# First date the accounting module posted anything. Invoices before this are
# imported iPos history with no cost or payment data and are deliberately
# excluded from the ledger — see repair_accounting_balance's module docstring.
GO_LIVE = date(2026, 6, 5)

# Invoices synced in from the legacy iPos POS carry this prefix. iPos kept running
# alongside CPMS through 2026-06-18, so 389 of them fall *after* GO_LIVE — but they
# are still imported rows with no cost or payment data, and during the parallel run
# it is not established whether they duplicate CPMS-native sales. They stay out of
# the ledger on the same grounds as the pre-go-live history. Posting them later is
# easy; unwinding a double-count is not.
IMPORTED_INVOICE_PREFIX = 'IPOS'

DOCUMENT_FIELDS = ('invoice', 'purchase_invoice', 'transfer')


def trial_balance():
    """Total debits and credits across the whole ledger."""
    dr = LedgerEntry.objects.filter(entry_type='debit').aggregate(t=Sum('amount'))['t'] or D('0')
    cr = LedgerEntry.objects.filter(entry_type='credit').aggregate(t=Sum('amount'))['t'] or D('0')
    return dr, cr


def unbalanced_documents(field):
    """IDs of documents of ``field`` whose own entries do not balance."""
    grouped = defaultdict(lambda: [D('0'), D('0')])
    for row in (LedgerEntry.objects.exclude(**{f'{field}__isnull': True})
                .values(f'{field}_id', 'entry_type').annotate(t=Sum('amount'))):
        idx = 0 if row['entry_type'] == 'debit' else 1
        grouped[row[f'{field}_id']][idx] += row['t'] or D('0')
    return [pk for pk, (dr, cr) in grouped.items() if dr != cr]


def derived_balance(account, sums=None):
    """The balance ``account`` should carry given its ledger rows."""
    if sums is None:
        dr = account.ledger_entries.filter(entry_type='debit').aggregate(t=Sum('amount'))['t'] or D('0')
        cr = account.ledger_entries.filter(entry_type='credit').aggregate(t=Sum('amount'))['t'] or D('0')
    else:
        dr, cr = sums
    return (dr - cr) if account.account_type in DEBIT_NORMAL_TYPES else (cr - dr)


def balance_drift():
    """[(account, stored, derived)] for accounts whose stored balance is stale."""
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
    """Value of stock still on the shelf, from the batch subledger.

    ``InventoryBatch.value`` is the batch total as received (migration 0074
    converted it from per-unit), so the remaining value is prorated by the
    quantity left.
    """
    total = D('0')
    for value, qi, qr in InventoryBatch.objects.values_list(
            'value', 'quantity_initial', 'quantity_remaining'):
        if not qi:
            continue
        total += (value * qr / qi)
    return total.quantize(D('0.01'))


def inventory_tie():
    """(gl_balance, on_hand_value, difference) for the inventory control account."""
    account = ChartOfAccounts.objects.filter(account_number=ACC_INVENTORY).first()
    gl = derived_balance(account) if account else D('0')
    on_hand = inventory_on_hand_value()
    return gl, on_hand, on_hand - gl


def invoices_missing_from_ledger(cutoff=GO_LIVE, include_imported=False):
    """Live, CPMS-native invoices on/after ``cutoff`` that never reached the ledger.

    iPos-imported rows are excluded unless ``include_imported`` is set — see
    ``IMPORTED_INVOICE_PREFIX``.
    """
    posted = LedgerEntry.objects.filter(invoice__isnull=False).values('invoice_id')
    qs = (Invoice.objects
          .filter(datetime__date__gte=cutoff, is_voided=False)
          .exclude(id__in=posted)
          # A wholly zero invoice (no lines, or only zero-priced ones) has no
          # journal entry to make. An invoice discounted to a zero grand_total
          # still does, so test the lines rather than grand_total alone.
          .annotate(line_gross=Sum(F('items__price') * F('items__quantity')))
          .exclude(grand_total=0, line_gross__isnull=True)
          .exclude(grand_total=0, line_gross=0))
    if not include_imported:
        qs = qs.exclude(invoice_number__startswith=IMPORTED_INVOICE_PREFIX)
    return qs


def non_reconciling_invoices(cutoff=GO_LIVE):
    """[(invoice, gap)] where the lines do not add up to ``grand_total``.

    A gap is absorbed into Sales Discount when posting, so it never unbalances
    the ledger — but it means a client sent line prices that disagree with the
    total it charged, which is worth surfacing.
    """
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
