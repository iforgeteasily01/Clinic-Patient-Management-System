from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum

from ..models import BankStatementLine, LedgerEntry

ZERO = Decimal('0')

AUTO_MATCH_WINDOW_DAYS = 5


class ReconciliationError(Exception):
    def __init__(self, errors):
        self.errors = errors if isinstance(errors, dict) else {'error': errors}
        super().__init__(str(self.errors))


def signed_amount(entry):
    amount = entry.amount or ZERO
    return amount if entry.entry_type == 'debit' else -amount


def book_entries(reconciliation):
    qs = (LedgerEntry.objects
          .filter(account_id=reconciliation.account_id,
                  date__gte=reconciliation.statement_start,
                  date__lte=reconciliation.statement_end)
          .select_related('invoice', 'purchase_invoice', 'expense', 'transfer',
                          'sales_return', 'journal_entry'))

    if reconciliation.branch_id:
        qs = qs.filter(Q(branch_id=reconciliation.branch_id) | Q(branch__isnull=True))

    qs = qs.filter(Q(reconciliation__isnull=True) | Q(reconciliation=reconciliation))

    return qs.order_by('date', 'id')


def book_balance_as_of(reconciliation):
    qs = LedgerEntry.objects.filter(
        account_id=reconciliation.account_id,
        date__lte=reconciliation.statement_end,
    )
    if reconciliation.branch_id:
        qs = qs.filter(Q(branch_id=reconciliation.branch_id) | Q(branch__isnull=True))

    agg = qs.aggregate(
        debit=Sum('amount', filter=Q(entry_type='debit')),
        credit=Sum('amount', filter=Q(entry_type='credit')),
    )
    return (agg['debit'] or ZERO) - (agg['credit'] or ZERO)


def describe_entry(entry):
    doc = None
    if entry.invoice_id:
        doc = entry.invoice.invoice_number
    elif entry.purchase_invoice_id:
        doc = entry.purchase_invoice.internal_id
    elif entry.sales_return_id:
        doc = entry.sales_return.return_number
    elif entry.expense_id:
        doc = f'Beban #{entry.expense_id}'
    elif entry.transfer_id:
        doc = f'Transfer #{entry.transfer_id}'
    return doc


def auto_match(reconciliation):
    lines = list(reconciliation.lines.filter(ledger_entry__isnull=True, is_ignored=False))
    if not lines:
        return 0

    candidates = list(book_entries(reconciliation))
    taken = set(
        reconciliation.lines
        .filter(ledger_entry__isnull=False)
        .values_list('ledger_entry_id', flat=True)
    )

    by_amount = {}
    for entry in candidates:
        by_amount.setdefault(signed_amount(entry), []).append(entry)

    matched = 0

    def claim(line, entry):
        nonlocal matched
        line.ledger_entry = entry
        line.match_type = BankStatementLine.MATCH_AUTO
        line.save(update_fields=['ledger_entry', 'match_type'])
        taken.add(entry.pk)
        matched += 1

    def unique_candidate(line, *, same_date):
        pool = [e for e in by_amount.get(line.amount, []) if e.pk not in taken]
        if same_date:
            pool = [e for e in pool if e.date == line.date]
        else:
            window = timedelta(days=AUTO_MATCH_WINDOW_DAYS)
            pool = [e for e in pool if abs(e.date - line.date) <= window]
        return pool[0] if len(pool) == 1 else None

    for same_date in (True, False):
        for line in lines:
            if line.ledger_entry_id is not None:
                continue
            entry = unique_candidate(line, same_date=same_date)
            if entry is not None:
                claim(line, entry)

    return matched


def match_line(reconciliation, line, entry):
    if reconciliation.is_locked:
        raise ReconciliationError({'error': 'Rekonsiliasi ini sudah diselesaikan.'})
    if line.reconciliation_id != reconciliation.pk:
        raise ReconciliationError({'line': 'Baris ini bukan bagian dari rekonsiliasi ini.'})
    if entry.account_id != reconciliation.account_id:
        raise ReconciliationError(
            {'entry': 'Transaksi ini bukan milik rekening yang direkonsiliasi.'}
        )
    if entry.reconciliation_id and entry.reconciliation_id != reconciliation.pk:
        raise ReconciliationError(
            {'entry': 'Transaksi ini sudah direkonsiliasi pada periode lain.'}
        )

    clash = (reconciliation.lines
             .filter(ledger_entry=entry)
             .exclude(pk=line.pk)
             .first())
    if clash is not None:
        raise ReconciliationError(
            {'entry': f'Transaksi ini sudah dicocokkan dengan baris {clash.date}.'}
        )

    line.ledger_entry = entry
    line.match_type = BankStatementLine.MATCH_MANUAL
    line.is_ignored = False
    line.save(update_fields=['ledger_entry', 'match_type', 'is_ignored'])
    return line


def unmatch_line(reconciliation, line):
    if reconciliation.is_locked:
        raise ReconciliationError({'error': 'Rekonsiliasi ini sudah diselesaikan.'})
    line.ledger_entry = None
    line.match_type = ''
    line.save(update_fields=['ledger_entry', 'match_type'])
    return line


def summary(reconciliation):
    lines = list(reconciliation.lines.select_related('ledger_entry'))
    entries = list(book_entries(reconciliation))

    matched_entry_ids = {l.ledger_entry_id for l in lines if l.ledger_entry_id}

    statement_total = sum((l.amount for l in lines if not l.is_ignored), ZERO)
    computed_closing = reconciliation.opening_balance + statement_total
    statement_drift = reconciliation.closing_balance - computed_closing

    unmatched_lines = [l for l in lines if not l.is_matched and not l.is_ignored]
    unmatched_entries = [e for e in entries if e.pk not in matched_entry_ids]

    book_balance = book_balance_as_of(reconciliation)
    difference = book_balance - reconciliation.closing_balance

    return {
        'book_balance':              book_balance,
        'statement_opening':         reconciliation.opening_balance,
        'statement_closing':         reconciliation.closing_balance,
        'statement_total':           statement_total,
        'statement_computed_closing': computed_closing,
        'statement_drift':           statement_drift,
        'difference':                difference,

        'line_count':                len(lines),
        'matched_count':             sum(1 for l in lines if l.is_matched),
        'ignored_count':             sum(1 for l in lines if l.is_ignored),
        'unmatched_line_count':      len(unmatched_lines),
        'unmatched_line_total':      sum((l.amount for l in unmatched_lines), ZERO),

        'book_entry_count':          len(entries),
        'unmatched_entry_count':     len(unmatched_entries),
        'unmatched_entry_total':     sum((signed_amount(e) for e in unmatched_entries), ZERO),

        'is_balanced':               difference == ZERO,
        'can_complete':              (difference == ZERO
                                      and not unmatched_lines
                                      and not unmatched_entries),
    }


def complete(reconciliation, actor):
    from django.utils import timezone

    if reconciliation.is_locked:
        raise ReconciliationError({'error': 'Rekonsiliasi ini sudah diselesaikan.'})

    figures = summary(reconciliation)
    if not figures['can_complete']:
        problems = []
        if figures['difference'] != ZERO:
            problems.append(f"selisih Rp{figures['difference']:,.2f}")
        if figures['unmatched_line_count']:
            problems.append(f"{figures['unmatched_line_count']} baris rekening koran belum cocok")
        if figures['unmatched_entry_count']:
            problems.append(f"{figures['unmatched_entry_count']} transaksi buku belum cocok")
        raise ReconciliationError(
            {'error': 'Rekonsiliasi belum seimbang: ' + ', '.join(problems) + '.'}
        )

    matched_ids = list(
        reconciliation.lines
        .filter(ledger_entry__isnull=False)
        .values_list('ledger_entry_id', flat=True)
    )
    LedgerEntry.objects.filter(pk__in=matched_ids).update(reconciliation=reconciliation)

    reconciliation.status = 'completed'
    reconciliation.completed_at = timezone.now()
    reconciliation.completed_by = actor
    reconciliation.save(update_fields=['status', 'completed_at', 'completed_by'])
    return reconciliation


def reopen(reconciliation):
    LedgerEntry.objects.filter(reconciliation=reconciliation).update(reconciliation=None)
    reconciliation.status = 'draft'
    reconciliation.completed_at = None
    reconciliation.completed_by = None
    reconciliation.save(update_fields=['status', 'completed_at', 'completed_by'])
    return reconciliation
