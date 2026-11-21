from decimal import Decimal

from ..models import ChartOfAccounts, ExpenseAlias
from .cash_accounts import cash_bank_account_ids
from .expense_create import create_expense


class AliasExpenseError(Exception):
    def __init__(self, errors):
        super().__init__(str(errors))
        self.errors = errors


def resolve_payment_account(payment_account_id):
    if payment_account_id not in cash_bank_account_ids():
        raise AliasExpenseError({'payment_account_id': ['Not a cash/bank account.']})
    account = ChartOfAccounts.objects.filter(pk=payment_account_id).first()
    if account is None:
        raise AliasExpenseError({'payment_account_id': ['Not a cash/bank account.']})
    return account


def create_alias_expense(*, data, scope, source, actor, status_override='paid', branch=None):
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
        branch=branch,
        status_override=status_override,
    )
