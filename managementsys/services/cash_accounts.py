"""Which ChartOfAccounts rows actually represent cash/bank locations.

``account_type='asset'`` is far too broad a filter for a "where did the money
leave from?" picker — inventory, accounts receivable and fixed assets are all
assets too. The clinic already curates the real list implicitly: every active
``PaymentMethod`` points at the account behind it. That curated set, unioned
with any account already used as ``Expense.payment_account`` (so an account
stays selectable even after its payment method is deactivated, and historical
rows never fail validation), is the authoritative answer.

Kept in its own module rather than inside ``journal_engine`` so the expense
views, the serializer and any future report/PDF can import it without pulling
in the whole posting engine.
"""
from ..models import Expense, PaymentMethod


def cash_bank_account_ids() -> set[int]:
    """The COA ids that count as real cash/bank accounts.

    Distinct ``PaymentMethod.linked_account`` ids for active payment methods,
    plus any id already referenced by ``Expense.payment_account``.
    """
    ids = set(
        PaymentMethod.objects
        .filter(is_active=True, linked_account__isnull=False)
        .values_list('linked_account_id', flat=True)
    )
    ids |= set(
        Expense.objects
        .filter(payment_account__isnull=False)
        .values_list('payment_account_id', flat=True)
    )
    ids.discard(None)
    return ids
