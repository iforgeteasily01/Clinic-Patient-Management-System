"""The single expense-creation code path.

``views.accounting_page.ExpenseListCreateView.post`` (the manager-facing form)
and the beautician petty-cash flow (``views.beautician_expense_page``) both
end here. Two divergent ways to write an ``Expense`` + its ``ExpenseItem``
rows is exactly the failure mode the beautician-expense design explicitly
warns against — see docs/stock-movement-patient-activity-design.md §4,
"Reuse, do not re-implement." Neither caller talks to the posting engine
directly; both just write an ordinary Expense and let the journal preview/
commit pick it up, same as any hand-entered one.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction

from ..models import AuditLog, Expense, ExpenseItem


def _safe_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError):
        return Decimal('0')


def create_expense(*, expense_date, payment_method, payment_account, payment_memo,
                    notes, amount_paid, items, actor, source='general', status_override=None):
    """Write one Expense + its ExpenseItem lines and audit-log it.

    ``items`` is a list of dicts with at least ``account`` (a ChartOfAccounts
    pk) and ``amount``; ``description`` and ``alias`` (an ExpenseAlias
    instance, or None) are optional. Runs in its own transaction so a caller
    that is not already inside one still gets atomicity.

    ``status_override`` lets the beautician flow force ``status='paid'``
    directly — petty cash is spent the moment it is logged, there is no
    payable — without this function having to infer intent from
    ``amount_paid`` vs. the computed total the way ``refresh_status()`` does
    for a hand-entered expense that may be partially paid.
    """
    with transaction.atomic():
        expense = Expense.objects.create(
            expense_date=expense_date,
            payment_method=payment_method,
            payment_account=payment_account,
            payment_memo=(payment_memo or '')[:255],
            notes=notes or '',
            amount_paid=_safe_decimal(amount_paid),
            created_by=actor,
            source=source,
        )

        item_objs = []
        total = Decimal('0')
        for row in items:
            amt = _safe_decimal(row.get('amount', 0))
            if amt <= 0:
                continue
            total += amt
            item_objs.append(ExpenseItem(
                expense=expense,
                account_id=row['account'],
                description=row.get('description', ''),
                amount=amt,
                alias=row.get('alias'),
            ))
        ExpenseItem.objects.bulk_create(item_objs)

        expense.total_amount = total.quantize(Decimal('0.01'))
        if status_override:
            expense.status = status_override
        else:
            expense.refresh_status()
        expense.save(update_fields=['total_amount', 'status'])

        # Phase 2/3: the accrual double-entry (Dr expense/cogs accounts, Cr AP
        # or payment account) is deferred, same as every other expense — this
        # row sits posting_status='unposted' with zero LedgerEntry rows until
        # a journal run sweeps its expense_date.

        AuditLog.objects.create(
            performed_by=actor,
            action='CREATE',
            entity_type='Expense',
            entity_id=str(expense.id),
            description=f'Expense created: #{expense.id} (Rp{expense.total_amount:,.0f})',
        )

    return expense
