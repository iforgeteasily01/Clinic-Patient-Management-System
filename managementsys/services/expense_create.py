from decimal import Decimal, InvalidOperation

from django.db import transaction

from ..models import AuditLog, Expense, ExpenseItem


def _safe_decimal(val) -> Decimal:
    try:
        return Decimal(str(val))
    except (InvalidOperation, TypeError):
        return Decimal('0')


def create_expense(*, expense_date, payment_method, payment_account, payment_memo,
                    notes, amount_paid, items, actor, source='general', status_override=None,
                    branch=None):
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
            branch=branch,
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


        AuditLog.objects.create(
            performed_by=actor,
            action='CREATE',
            entity_type='Expense',
            entity_id=str(expense.id),
            description=f'Expense created: #{expense.id} (Rp{expense.total_amount:,.0f})',
        )

    return expense
