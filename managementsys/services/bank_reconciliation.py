"""Bank reconciliation: the book side, the matching, and the arithmetic.

The whole feature answers one question — *does the ledger agree with the bank,
and if not, which transactions explain the gap?* — and everything here exists to
make that gap enumerable instead of a single unexplained number.

Three things worth reading before changing any of it.

**Signs are normalised once, at the edge.** A ``LedgerEntry`` on an asset account
is a debit (money in) or a credit (money out); a bank statement is two columns
whose names differ per bank. Both are converted to one signed number —
positive in, negative out — by ``signed_amount`` and by the import. Every
comparison after that is plain arithmetic, which is the only way the matching
stays comprehensible.

**Nothing here writes to the ledger.** A reconciliation is an assertion *about*
the books, never a change to them. A bank charge nobody recorded is fixed by
entering an expense, not by the reconciler inventing a journal line. If that
rule ever bends, reconciliation stops being evidence and becomes a second,
unreviewed posting path.

**Auto-matching refuses ambiguity.** A statement line with two equally good
candidates is left unmatched rather than matched to the first one. A wrong match
is far more expensive than an unmatched line: the unmatched line is visible and
someone deals with it, while the wrong match balances the period and hides a
real discrepancy until an audit.
"""
from datetime import timedelta
from decimal import Decimal

from django.db.models import Q, Sum

from ..models import BankStatementLine, LedgerEntry

ZERO = Decimal('0')

# How far apart a statement date and a book date may be and still auto-match.
# Card settlements and transfers commonly clear a day or two after they are
# recorded; beyond a few days the amount alone stops being good evidence.
AUTO_MATCH_WINDOW_DAYS = 5


class ReconciliationError(Exception):
    """Something the reconciler must not be allowed to do. DRF-shaped errors."""

    def __init__(self, errors):
        self.errors = errors if isinstance(errors, dict) else {'error': errors}
        super().__init__(str(self.errors))


# ── The book side ─────────────────────────────────────────────────────────────

def signed_amount(entry):
    """A ledger row's effect on a cash/bank account, positive in, negative out.

    Only meaningful for asset accounts, which is all this module ever handles:
    a debit raises them and a credit lowers them.
    """
    amount = entry.amount or ZERO
    return amount if entry.entry_type == 'debit' else -amount


def book_entries(reconciliation):
    """Ledger rows on this account within the statement period.

    Rows already cleared by a *different* reconciliation are always excluded:
    they were settled in an earlier period, and offering them again is how the
    same transaction gets reconciled twice — and, worse, how they show up here
    as "unmatched book entries" that no match can ever clear, permanently
    blocking completion.

    Rows cleared by *this* reconciliation are always included: they are its own
    matches, and a period cannot review what it cannot see.

    There is deliberately no flag to widen this. The two rules above are not
    situational.
    """
    qs = (LedgerEntry.objects
          .filter(account_id=reconciliation.account_id,
                  date__gte=reconciliation.statement_start,
                  date__lte=reconciliation.statement_end)
          .select_related('invoice', 'purchase_invoice', 'expense', 'transfer',
                          'sales_return', 'journal_entry'))

    if reconciliation.branch_id:
        # Null-branch rows ride along for the same reason they do in the
        # reports: pre-multi-branch history and genuinely group-wide entries
        # still hit this bank account, and excluding them would guarantee an
        # unexplainable difference.
        qs = qs.filter(Q(branch_id=reconciliation.branch_id) | Q(branch__isnull=True))

    qs = qs.filter(Q(reconciliation__isnull=True) | Q(reconciliation=reconciliation))

    return qs.order_by('date', 'id')


def book_balance_as_of(reconciliation):
    """The account's ledger balance at the end of the statement period.

    Computed from the ledger rows rather than read off
    ``ChartOfAccounts.balance``: the cached balance is as-of-now, and a
    reconciliation asks about a date in the past.
    """
    qs = LedgerEntry.objects.filter(
        account_id=reconciliation.account_id,
        date__lte=reconciliation.statement_end,
    )
    if reconciliation.branch_id:
        qs = qs.filter(Q(branch_id=reconciliation.branch_id) | Q(branch__isnull=True))

    # Aggregated in the database rather than walked in Python: a bank account
    # accumulates every sale, and pulling several years of rows across the wire
    # to add them up would make the reconciliation screen slower every month.
    agg = qs.aggregate(
        debit=Sum('amount', filter=Q(entry_type='debit')),
        credit=Sum('amount', filter=Q(entry_type='credit')),
    )
    return (agg['debit'] or ZERO) - (agg['credit'] or ZERO)


def describe_entry(entry):
    """A human label for one book row, naming the document behind it.

    The description on the ledger line already carries the invoice or expense
    number, but the document type is what an operator scans for when hunting a
    missing match, so it is surfaced separately rather than buried in prose.
    """
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


# ── Matching ──────────────────────────────────────────────────────────────────

def auto_match(reconciliation):
    """Match every statement line that has exactly one credible candidate.

    Two passes, and the order matters:

      1. **Same date, same amount.** The strongest evidence available without
         parsing bank narratives, which are unreliable across banks and not
         worth the false-positive risk.
      2. **Amount within the date window.** Weaker, so it only gets to run on
         lines the first pass could not settle.

    Within either pass, a line with more than one candidate is skipped. So is a
    candidate already claimed by another line in this same run — otherwise two
    identical Rp 500.000 deposits both match the same book row and the period
    balances against a transaction that happened once.

    Returns the number of lines matched.
    """
    lines = list(reconciliation.lines.filter(ledger_entry__isnull=True, is_ignored=False))
    if not lines:
        return 0

    candidates = list(book_entries(reconciliation))
    # Book rows already spoken for, either by a previous run or by this one.
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
        # Exactly one, or nothing. Ambiguity is left for a human — see the
        # module docstring.
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
    """Match one statement line to one ledger row, by hand.

    Every rule the auto-matcher enforces implicitly is enforced explicitly here,
    because this is the path a determined operator uses to force a match that
    the automatic pass refused.
    """
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

    # A forced match with a different amount is allowed and deliberately so: a
    # bank fee netted into a transfer is a real thing, and refusing it would
    # push the operator into editing the statement to make it fit. The summary
    # reports the resulting difference rather than hiding it.
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


# ── The arithmetic ────────────────────────────────────────────────────────────

def summary(reconciliation):
    """Every figure the reconciliation screen shows, and how they relate.

    ``difference`` is the headline: the ledger balance at period end minus what
    the bank says it should be. Zero means reconciled. Anything else is
    explained by the two enumerable lists beneath it —

      * statement lines with no book entry (the clinic has not recorded it), and
      * book entries with no statement line (it has not cleared the bank yet, or
        it was recorded twice).

    ``statement_drift`` is a separate, narrower check: whether the imported
    lines actually add up from the stated opening balance to the stated closing
    balance. A non-zero drift means the *import* is incomplete, and chasing the
    main difference before fixing it is wasted effort — which is why it is
    reported on its own rather than folded into the total.
    """
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
    """Close the period and stamp every matched ledger row as cleared.

    Refused unless the reconciliation actually balances *and* nothing is left
    unexplained on either side. A completed reconciliation that does not balance
    is worse than an open one: it looks finished, so nobody comes back to it.

    The stamp on ``LedgerEntry.reconciliation`` is what makes the next period
    correct — a transaction cleared here is never offered as a candidate again.
    """
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
    """Undo a completion, releasing every ledger row it cleared.

    Needed because a reconciliation can be completed on a mistaken match and the
    only honest fix is to open it back up. Releasing the stamp is the whole of
    it — the matches themselves are kept so the operator resumes where they were
    rather than starting the period again.
    """
    LedgerEntry.objects.filter(reconciliation=reconciliation).update(reconciliation=None)
    reconciliation.status = 'draft'
    reconciliation.completed_at = None
    reconciliation.completed_by = None
    reconciliation.save(update_fields=['status', 'completed_at', 'completed_by'])
    return reconciliation
