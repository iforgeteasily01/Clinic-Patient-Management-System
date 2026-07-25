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

from decimal import Decimal
from django.db.models import Q, Sum

from ..models import ChartOfAccounts, LedgerEntry

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

# Cash / bank asset accounts for the simplified cash-flow statement.
CASH_ACCOUNT_NUMBERS = (1100000, 1110000)


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


def accounts_by_id():
    """All accounts keyed by pk, with the light fields the reports render."""
    return {
        a.id: a
        for a in ChartOfAccounts.objects.all().only(
            'id', 'account_number', 'name', 'account_type', 'is_head', 'parent_id',
        )
    }


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
