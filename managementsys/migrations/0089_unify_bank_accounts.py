from decimal import Decimal

from django.db import migrations
from django.db.models import F
from django.utils import timezone


CUTOVER_DESCRIPTION = 'Penggabungan rekening bank — konsolidasi ke rekening utama'
REFERENCE_PREFIX = 'MIGRASI-0089'
INACTIVE_SUFFIX = ' (nonaktif)'

RENAMES = {
    1100002: 'Bank BCA',
    1100005: 'Bank Mandiri',
}
ORIGINAL_NAMES = {
    1100002: 'Transfer BCA',
    1100005: 'QRIS Mandiri',
}

CONSOLIDATION = {
    1100003: 1100002,
    1100004: 1100002,
    1100006: 1100005,
    1100007: 1100005,
}


def unify_bank_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    AccountTransfer = apps.get_model('managementsys', 'AccountTransfer')

    if AccountTransfer.objects.filter(reference__startswith=REFERENCE_PREFIX).exists():
        return

    numbers = set(RENAMES) | set(CONSOLIDATION) | set(CONSOLIDATION.values())
    accounts = {a.account_number: a
                for a in ChartOfAccounts.objects.filter(account_number__in=numbers)}

    if not all(n in accounts for n in numbers):
        return

    balances_before = {n: accounts[n].balance for n in numbers}
    total_before = sum(balances_before[n] for n in set(CONSOLIDATION) | set(CONSOLIDATION.values()))

    for number, new_name in RENAMES.items():
        ChartOfAccounts.objects.filter(pk=accounts[number].pk).update(name=new_name)
    for number in CONSOLIDATION:
        name = accounts[number].name
        if not name.endswith(INACTIVE_SUFFIX):
            ChartOfAccounts.objects.filter(pk=accounts[number].pk).update(
                name=name + INACTIVE_SUFFIX)

    for source_number, destination_number in CONSOLIDATION.items():
        PaymentMethod.objects.filter(
            linked_account__account_number=source_number
        ).update(linked_account=accounts[destination_number].pk)

    cutover = timezone.now().date()
    for source_number, destination_number in CONSOLIDATION.items():
        amount = balances_before[source_number]
        if amount == 0:
            continue
        source = accounts[source_number]
        destination = accounts[destination_number]
        transfer = AccountTransfer.objects.create(
            transfer_date=cutover,
            from_account=source,
            to_account=destination,
            amount=amount,
            description=CUTOVER_DESCRIPTION,
            reference=f'{REFERENCE_PREFIX}-{source_number}',
            created_by=None,
            posting_status='posted',
        )
        LedgerEntry.objects.bulk_create([
            LedgerEntry(
                account=source, date=cutover, description=CUTOVER_DESCRIPTION,
                entry_type='credit', amount=amount,
                source_type='transfer', transfer=transfer,
            ),
            LedgerEntry(
                account=destination, date=cutover, description=CUTOVER_DESCRIPTION,
                entry_type='debit', amount=amount,
                source_type='transfer', transfer=transfer,
            ),
        ])
        ChartOfAccounts.objects.filter(pk=source.pk).update(balance=F('balance') - amount)
        ChartOfAccounts.objects.filter(pk=destination.pk).update(balance=F('balance') + amount)

    after = {a.account_number: a.balance
             for a in ChartOfAccounts.objects.filter(account_number__in=numbers)}
    for source_number in CONSOLIDATION:
        if after[source_number] != Decimal('0'):
            raise RuntimeError(
                f'0089: account {source_number} did not drain to zero '
                f'(left {after[source_number]})'
            )
    total_after = sum(after[n] for n in set(CONSOLIDATION.values()))
    if total_after != total_before:
        raise RuntimeError(
            f'0089: consolidated balance {total_after} != pre-migration total {total_before}'
        )


def restore_bank_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    AccountTransfer = apps.get_model('managementsys', 'AccountTransfer')

    transfers = list(
        AccountTransfer.objects.filter(reference__startswith=REFERENCE_PREFIX)
        .select_related('from_account', 'to_account')
    )

    for transfer in transfers:
        LedgerEntry.objects.filter(transfer=transfer).delete()
        ChartOfAccounts.objects.filter(pk=transfer.from_account_id).update(
            balance=F('balance') + transfer.amount)
        ChartOfAccounts.objects.filter(pk=transfer.to_account_id).update(
            balance=F('balance') - transfer.amount)

    for number in CONSOLIDATION:
        account = ChartOfAccounts.objects.filter(account_number=number).first()
        if account is not None and account.name.endswith(INACTIVE_SUFFIX):
            original = account.name[:-len(INACTIVE_SUFFIX)]
            ChartOfAccounts.objects.filter(pk=account.pk).update(name=original)
            PaymentMethod.objects.filter(name=original).update(linked_account=account.pk)

    for number, original in ORIGINAL_NAMES.items():
        ChartOfAccounts.objects.filter(account_number=number).update(name=original)

    AccountTransfer.objects.filter(pk__in=[t.pk for t in transfers]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0088_patientnote_visit_and_author'),
    ]

    operations = [
        migrations.RunPython(unify_bank_accounts, restore_bank_accounts),
    ]
