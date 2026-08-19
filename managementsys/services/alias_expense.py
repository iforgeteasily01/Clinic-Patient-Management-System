"""Write an Expense from alias-picked rows, for the staff-facing quick forms.

Two screens enter expenses without ever showing a GL account: the beautician
petty-cash form (scope='beautician') and the admin quick-purchase form
(scope='general'). Both resolve friendly ``ExpenseAlias`` names to real
accounts and then hand off to ``expense_create.create_expense`` — the same
single write path the manager's full expense form uses.

The resolution lives here rather than in either view so the two cannot drift.
The only thing that differs between them is which alias ``scope`` is allowed
and what ``Expense.source`` gets stamped; everything else — the memo fallback,
the paid-immediately status, the cash-account validation — is identical, and a
second copy of it would eventually stop being identical.
"""
from decimal import Decimal

from ..models import ChartOfAccounts, ExpenseAlias
from .cash_accounts import cash_bank_account_ids
from .expense_create import create_expense


class AliasExpenseError(Exception):
    """Validation failure, carrying a DRF-shaped ``errors`` dict."""

    def __init__(self, errors):
        super().__init__(str(errors))
        self.errors = errors


def resolve_payment_account(payment_account_id):
    """The cash/bank COA the money leaves from, or raise.

    Checked against the curated cash/bank set rather than just
    ``account_type='asset'`` — paying petty cash out of Accounts Receivable is
    an asset too, and would post a journal nobody can explain.
    """
    if payment_account_id not in cash_bank_account_ids():
        raise AliasExpenseError({'payment_account_id': ['Not a cash/bank account.']})
    account = ChartOfAccounts.objects.filter(pk=payment_account_id).first()
    if account is None:
        raise AliasExpenseError({'payment_account_id': ['Not a cash/bank account.']})
    return account


def create_alias_expense(*, data, scope, source, actor, status_override='paid'):
    """Resolve alias rows to accounts and write one Expense.

    ``data`` is validated output from a serializer shaped like
    ``BeauticianExpenseCreateSerializer``: ``expense_date``,
    ``payment_account_id``, optional ``payment_memo``/``notes``, and ``items``
    of ``{alias_id, amount, description?}``.

    Only active aliases in ``scope`` are accepted, so retiring an alias stops
    new spend against it without touching the rows already written through it.
    """
    payment_account = resolve_payment_account(data['payment_account_id'])

    alias_ids = [row['alias_id'] for row in data['items']]
    aliases = {
        a.id: a
        for a in ExpenseAlias.objects
        .filter(id__in=alias_ids, scope=scope, is_active=True)
        .select_related('account')
    }
    missing = [aid for aid in alias_ids if aid not in aliases]
    if missing:
        raise AliasExpenseError(
            {'items': f'Alias tidak ditemukan atau tidak aktif: {missing}'}
        )

    items = []
    for row in data['items']:
        alias = aliases[row['alias_id']]
        # description falls back to the alias name, so the journal memo reads
        # "Beli kapas" and the accountant can still see which account it hit,
        # even though the person entering it never saw an account number.
        items.append({
            'account': alias.account_id,
            'description': (row.get('description') or '').strip() or alias.name,
            'amount': row['amount'],
            'alias': alias,
        })

    total_paid = sum((row['amount'] for row in items), Decimal('0'))

    return create_expense(
        expense_date=data['expense_date'],
        payment_method=None,
        payment_account=payment_account,
        payment_memo=data.get('payment_memo', ''),
        notes=data.get('notes', ''),
        amount_paid=total_paid,
        items=items,
        actor=actor,
        source=source,
        # Paid immediately: these forms record money already spent, so there is
        # no payable to track. A liability that needs tracking goes through
        # /accounting/purchases (per-vendor AP) or the full expense form.
        status_override=status_override,
    )
