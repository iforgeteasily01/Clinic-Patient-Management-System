from ..models import ChartOfAccounts, Expense, PaymentMethod, PurchasePayment

CASH_HEAD_NUMBER = 1100000
CASH_BAND_END    = CASH_HEAD_NUMBER + 100000

RETIRED_SUFFIX = ' (nonaktif)'

RETIRED_CLEARING_NUMBER = 1100011


def cash_bank_account_ids() -> set[int]:
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
    ids -= set(
        ChartOfAccounts.objects
        .filter(account_number=RETIRED_CLEARING_NUMBER)
        .values_list('id', flat=True)
    )
    return ids
