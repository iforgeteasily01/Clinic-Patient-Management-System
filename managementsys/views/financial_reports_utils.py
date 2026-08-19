"""
Shared computation for the financial reports (Laba Rugi, Neraca, Neraca Saldo,
Buku Besar, Arus Kas).

Every report reads from the LedgerEntry journal — never from the cached
ChartOfAccounts.balance — so figures are period-accurate and the Trial Balance
is a genuine debit==credit check.

Sign convention
---------------
For a date window, an account's raw movement is:  net = Σ(debit) − Σ(credit).
`signed_balance()` flips that to the account's *natural* sign so a normal
liability/revenue/equity balance reads positive.
"""

from datetime import timedelta
from decimal import Decimal
from django.db.models import Q, Sum

from ..models import ChartOfAccounts, JournalDayLog, LedgerEntry

ZERO = Decimal('0')

# account_type → the side that increases the account
NORMAL_BALANCE = {
    'asset':         'debit',
    'cogs':          'debit',
    'expense':       'debit',
    'other_expense': 'debit',
    'liability':     'credit',
    'equity':        'credit',
    'revenue':       'credit',
    'other_income':  'credit',
}

# Account types that make up the Income Statement (used to derive earnings)
PL_TYPES = ('revenue', 'cogs', 'expense', 'other_income', 'other_expense')

# ── Cash-flow scope & activity classification ────────────────────────────────
#
# The cash-flow statement needs a *reporting* definition of "cash", which is
# deliberately not the same set as ``services.cash_accounts.cash_bank_account_ids``:
# that one answers "where may an operator pay from?" and so drops accounts that
# were retired from the pickers. A report must still show the money that moved
# through a retired account while it was live, or opening + net change no longer
# reconciles to the closing balance.
#
# So the report scope is: every non-head account in the 11xxxxx band (retired
# ``(nonaktif)`` instrument accounts included), plus anything referenced as a
# payment destination, minus the iPos history clearing account. That last
# exclusion is the same decision migration 0100 recorded — 1100011 holds the
# imported iPos sales history and nothing else, and its Rp 11.6bn balance is an
# artifact of that import, not cash the clinic can spend.

CASH_HEAD_NUMBER = 1100000
CASH_BAND_END = CASH_HEAD_NUMBER + 100000
IPOS_CLEARING_NUMBER = 1100011

ACTIVITY_OPERATING = 'operating'
ACTIVITY_INVESTING = 'investing'
ACTIVITY_FINANCING = 'financing'
ACTIVITY_ORDER = (ACTIVITY_OPERATING, ACTIVITY_INVESTING, ACTIVITY_FINANCING)

ACTIVITY_LABELS = {
    ACTIVITY_OPERATING: 'Arus Kas dari Aktivitas Operasi',
    ACTIVITY_INVESTING: 'Arus Kas dari Aktivitas Investasi',
    ACTIVITY_FINANCING: 'Arus Kas dari Aktivitas Pendanaan',
}

# Where the balance-sheet bands split current from non-current. The COA numbers
# assets 1000000+ (cash 11xxxxx, inventory 1300000) and liabilities 2000000+
# (accounts payable 21xxxxx, tax payable 2200000). Anything numbered at or above
# these cut-offs is treated as non-current, which is what makes a fixed-asset
# purchase investing and a bank loan financing once such accounts are added.
NON_CURRENT_ASSET_FROM = 1500000
LONG_TERM_LIABILITY_FROM = 2500000


def cash_report_account_ids():
    """The COA ids the cash-flow statement treats as cash — see the note above.

    Returns a set. Imported lazily inside the function because
    ``services.cash_accounts`` imports models that in turn pull view helpers."""
    from ..services.cash_accounts import cash_bank_account_ids

    ids = set(
        ChartOfAccounts.objects
        .filter(
            account_type='asset',
            is_head=False,
            account_number__gt=CASH_HEAD_NUMBER,
            account_number__lt=CASH_BAND_END,
        )
        .values_list('id', flat=True)
    )
    ids |= set(cash_bank_account_ids())
    ids -= set(
        ChartOfAccounts.objects
        .filter(account_number=IPOS_CLEARING_NUMBER)
        .values_list('id', flat=True)
    )
    return ids


def classify_activity(account):
    """Which cash-flow activity a *counterpart* account attributes its cash
    movement to. `account` is the non-cash side of the journal entry.

    P&L accounts are operating by definition. Working-capital accounts —
    receivables, inventory, payables, tax payable — are operating too, because
    the cash they move is trading cash. Equity and long-term debt are financing;
    non-current assets are investing. Returns None for an account this cannot
    place, so the caller can surface it rather than guess."""
    t = account.account_type
    if t in PL_TYPES:
        return ACTIVITY_OPERATING
    if t == 'asset':
        return ACTIVITY_INVESTING if account.account_number >= NON_CURRENT_ASSET_FROM else ACTIVITY_OPERATING
    if t == 'liability':
        return ACTIVITY_FINANCING if account.account_number >= LONG_TERM_LIABILITY_FROM else ACTIVITY_OPERATING
    if t == 'equity':
        return ACTIVITY_FINANCING
    return None


def signed_balance(account_type, net):
    """Return `net` for debit-normal accounts, `-net` for credit-normal ones,
    so a healthy balance of any account type reads as a positive number."""
    net = net or ZERO
    return net if NORMAL_BALANCE.get(account_type) == 'debit' else -net


def account_movements(date_from=None, date_to=None, account_ids=None):
    """
    Aggregate debit/credit totals per account over an (inclusive) date window.

    Returns: dict[account_id] -> {'debit': Decimal, 'credit': Decimal, 'net': Decimal}
    where net = debit - credit. Accounts with no activity are simply absent.
    """
    qs = LedgerEntry.objects.all()
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    if account_ids is not None:
        qs = qs.filter(account_id__in=account_ids)

    rows = qs.values('account_id').annotate(
        debit=Sum('amount', filter=Q(entry_type='debit')),
        credit=Sum('amount', filter=Q(entry_type='credit')),
    )

    out = {}
    for r in rows:
        debit = r['debit'] or ZERO
        credit = r['credit'] or ZERO
        out[r['account_id']] = {
            'debit': debit,
            'credit': credit,
            'net': debit - credit,
        }
    return out


def opening_balances(before_date, account_ids=None):
    """Net balance (debit - credit) of each account for all entries strictly
    before `before_date`. Returns dict[account_id] -> Decimal net."""
    qs = LedgerEntry.objects.filter(date__lt=before_date)
    if account_ids is not None:
        qs = qs.filter(account_id__in=account_ids)

    rows = qs.values('account_id').annotate(
        debit=Sum('amount', filter=Q(entry_type='debit')),
        credit=Sum('amount', filter=Q(entry_type='credit')),
    )
    return {r['account_id']: (r['debit'] or ZERO) - (r['credit'] or ZERO) for r in rows}


def _natural_sign(account_type):
    """+1 when the account grows on the debit side, -1 when it grows on the
    credit side. Derived from `signed_balance` itself so the two can never
    drift apart — do not hardcode a second list of credit-natural types."""
    return 1 if signed_balance(account_type, Decimal('1')) > 0 else -1


def ledger_rows_with_balance(account, date_from=None, date_to=None, entry_type=''):
    """Ascending ledger rows for one account, each carrying its running balance.

    Returns ``(rows, opening, closing, total_debit, total_credit)`` where
    ``rows`` is a list of ``LedgerEntry`` instances, each with a
    ``running_balance`` Decimal attached (the account's balance *after* that
    entry, in the account's natural sign).

    Ordering is ``date, created_at, id`` — ASCENDING. ``LedgerEntry.Meta``
    orders descending; a running balance walked over a descending queryset is
    silently wrong.

    ``opening`` is the natural-signed balance of everything strictly before
    ``date_from``; it is ZERO when no ``date_from`` is given (the ledger then
    starts at the account's very first entry).

    ``entry_type`` ('debit' | 'credit') is a *display* filter applied only
    after the walk, so every surviving row still shows its true balance within
    the full sequence — what an accountant expects. ``closing`` is likewise
    the balance after every entry in the window, filtered or not.
    ``total_debit`` / ``total_credit`` cover the displayed rows only.

    Shared by AccountLedgerView and the ledger PDF.
    """
    qs = (
        LedgerEntry.objects
        .filter(account=account)
        .select_related('invoice', 'purchase_invoice', 'transfer', 'expense')
    )
    if date_from:
        qs = qs.filter(date__gte=date_from)
    if date_to:
        qs = qs.filter(date__lte=date_to)
    qs = qs.order_by('date', 'created_at', 'id')

    if date_from:
        raw_opening = opening_balances(date_from, account_ids=[account.pk]).get(account.pk, ZERO)
        opening = signed_balance(account.account_type, raw_opening)
    else:
        opening = ZERO

    sign = _natural_sign(account.account_type)

    rows = []
    running = opening
    for e in qs:
        delta = e.amount if e.entry_type == 'debit' else -e.amount
        running += delta * sign
        e.running_balance = running
        rows.append(e)

    closing = running

    if entry_type in ('debit', 'credit'):
        rows = [e for e in rows if e.entry_type == entry_type]

    total_debit = sum((e.amount for e in rows if e.entry_type == 'debit'), ZERO)
    total_credit = sum((e.amount for e in rows if e.entry_type == 'credit'), ZERO)

    return rows, opening, closing, total_debit, total_credit


def accounts_by_id():
    """All accounts keyed by pk, with the light fields the reports render."""
    return {
        a.id: a
        for a in ChartOfAccounts.objects.all().only(
            'id', 'account_number', 'name', 'account_type', 'is_head', 'parent_id',
        )
    }


def earliest_ledger_date():
    """The date of the oldest LedgerEntry row, or None if the journal is empty.
    Used by as_of-style reports (Trial Balance, Balance Sheet) to bound the
    'requested range' they check for unposted days — they have no explicit
    date_from, but their figures are cumulative from the start of the journal."""
    return LedgerEntry.objects.order_by('date').values_list('date', flat=True).first()


def unposted_dates_in_range(date_from, date_to):
    """Every calendar date in [date_from, date_to] (inclusive) that is not
    marked is_posted=True in JournalDayLog — a date with no row at all counts
    as unposted (never swept). Returns a sorted list of date objects; empty
    when the range is empty/invalid or every day in it has been posted."""
    if date_from is None or date_to is None or date_from > date_to:
        return []
    posted = set(
        JournalDayLog.objects
        .filter(date__gte=date_from, date__lte=date_to, is_posted=True)
        .values_list('date', flat=True)
    )
    out = []
    cur = date_from
    while cur <= date_to:
        if cur not in posted:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def earnings_through(as_of):
    """Net profit accumulated from the beginning of time through `as_of`
    (inclusive). Positive = profit. Used to inject current/retained earnings
    into equity on the Balance Sheet without a formal period close."""
    mv = account_movements(date_to=as_of)
    accts = accounts_by_id()
    total = ZERO
    for acc_id, m in mv.items():
        acc = accts.get(acc_id)
        if acc and acc.account_type in PL_TYPES:
            # revenue/other_income are credit-normal (+), cogs/expense debit-normal (−)
            total += signed_balance(acc.account_type, m['net']) * (
                1 if acc.account_type in ('revenue', 'other_income') else -1
            )
    return total
