"""Which ChartOfAccounts rows actually represent cash/bank locations.

``account_type='asset'`` is far too broad a filter for a "where did the money
leave from?" picker — inventory, accounts receivable and fixed assets are all
assets too. The clinic curates the real list structurally: every cash, bank and
e-wallet account is a sub-account of the ``1100000 Cash & Payment Accounts``
head, so the 11xxxxx band *is* the definition.

That band is unioned with any account already referenced as a payment
destination — ``PaymentMethod.linked_account`` (active methods),
``Expense.payment_account`` and ``PurchasePayment.payment_account`` — so an
account stays selectable even if it was later renumbered outside the band, and
historical rows never fail validation.

Kept in its own module rather than inside ``journal_engine`` so the expense
views, the purchase views, the serializers and any future report/PDF can import
it without pulling in the whole posting engine.
"""
from ..models import ChartOfAccounts, Expense, PaymentMethod, PurchasePayment

# The head that every cash/bank/e-wallet account hangs off, and the numbering
# band its children live in (1100001 … 1199999).
CASH_HEAD_NUMBER = 1100000
CASH_BAND_END    = CASH_HEAD_NUMBER + 100000

# ChartOfAccounts has no is_active flag, so migration 0089 retired the drained
# per-instrument bank accounts by suffixing their names. Those are still real
# accounts holding real history — they just must not be offered as somewhere to
# pay from or transfer to.
RETIRED_SUFFIX = ' (nonaktif)'

# 1100011 was the "Undeposited Funds" clearing account (migration 0076). It holds
# the cash side of the imported iPos history and nothing else, so migration 0100
# renamed it and retired it. Excluded by number rather than by name so a future
# rename cannot quietly put it back in the pickers.
RETIRED_CLEARING_NUMBER = 1100011


def cash_bank_account_ids() -> set[int]:
    """The COA ids that count as real cash/bank accounts.

    Every non-head, non-retired asset sub-account in the ``1100000`` band, plus
    any id already referenced as a payment destination by a payment method, an
    expense or a purchase payment.
    """
    ids = set(
        ChartOfAccounts.objects
        .filter(
            account_type='asset',
            is_head=False,
            account_number__gt=CASH_HEAD_NUMBER,
            account_number__lt=CASH_BAND_END,
        )
        .exclude(name__endswith=RETIRED_SUFFIX)
        .exclude(account_number=RETIRED_CLEARING_NUMBER)
        .values_list('id', flat=True)
    )
    ids |= set(
        PaymentMethod.objects
        .filter(is_active=True, linked_account__isnull=False)
        .values_list('linked_account_id', flat=True)
    )
    ids |= set(
        Expense.objects
        .filter(payment_account__isnull=False)
        .values_list('payment_account_id', flat=True)
    )
    ids |= set(
        PurchasePayment.objects
        .filter(payment_account__isnull=False)
        .values_list('payment_account_id', flat=True)
    )
    ids.discard(None)
    # After the unions, not before: the clearing account still has a (now
    # inactive) payment method and could be re-added by a historical reference.
    ids -= set(
        ChartOfAccounts.objects
        .filter(account_number=RETIRED_CLEARING_NUMBER)
        .values_list('id', flat=True)
    )
    return ids
