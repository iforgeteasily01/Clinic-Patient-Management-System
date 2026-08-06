"""Unify the per-instrument BCA and Mandiri asset accounts into one account per bank.

The ``PaymentMethod`` cut-over (0080–0082) decoupled *how* a customer paid from
*which* GL account the money lands in, but the COA kept its pre-cut-over shape:
one asset account per payment instrument. Transfer/Debit/Kredit BCA are not three
pots of money — they are three rails into a single BCA bank account, and likewise
for QRIS/Debit/Kredit Mandiri. The instrument distinction belongs on
``PaymentMethod`` and nowhere else.

Why a transfer journal rather than repointing history
-----------------------------------------------------
Rewriting every historic ``LedgerEntry`` from 1100003 onto 1100002 would give a
tidier end state, but it silently restates every already-closed period — the July
trial balance would stop matching the July report that has already been printed.
``AccountTransfer`` exists precisely to move money between asset accounts with an
audit trail (``posting_status`` + ``LedgerEntry.source_type='transfer'`` already
model it), so four dated transfers on the cut-over date leave history intact and
stay individually reversible. The cost is four permanently-zero accounts;
``ChartOfAccounts`` has no ``is_active`` flag, so they carry a ``" (nonaktif)"``
name suffix instead.
"""
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

# drained account  ->  destination account
CONSOLIDATION = {
    1100003: 1100002,   # Debit BCA      -> Bank BCA
    1100004: 1100002,   # Kredit BCA     -> Bank BCA
    1100006: 1100005,   # Debit Mandiri  -> Bank Mandiri
    1100007: 1100005,   # Kredit Mandiri -> Bank Mandiri
}


def unify_bank_accounts(apps, schema_editor):
    ChartOfAccounts = apps.get_model('managementsys', 'ChartOfAccounts')
    PaymentMethod = apps.get_model('managementsys', 'PaymentMethod')
    LedgerEntry = apps.get_model('managementsys', 'LedgerEntry')
    AccountTransfer = apps.get_model('managementsys', 'AccountTransfer')

    # 1. Idempotency — a partial re-run must never double-transfer.
    if AccountTransfer.objects.filter(reference__startswith=REFERENCE_PREFIX).exists():
        return

    numbers = set(RENAMES) | set(CONSOLIDATION) | set(CONSOLIDATION.values())
    accounts = {a.account_number: a
                for a in ChartOfAccounts.objects.filter(account_number__in=numbers)}

    # Nothing to do on a database that never carried the per-instrument shape
    # (a fresh test DB, or a deployment seeded after this migration).
    if not all(n in accounts for n in numbers):
        return

    # Snapshot the pre-migration balances before any write, for the §5 assertion
    # and so the transfer amounts are read live rather than hardcoded.
    balances_before = {n: accounts[n].balance for n in numbers}
    total_before = sum(balances_before[n] for n in set(CONSOLIDATION) | set(CONSOLIDATION.values()))

    # 2. Rename the survivors; flag the drained accounts.
    for number, new_name in RENAMES.items():
        ChartOfAccounts.objects.filter(pk=accounts[number].pk).update(name=new_name)
    for number in CONSOLIDATION:
        name = accounts[number].name
        if not name.endswith(INACTIVE_SUFFIX):
            ChartOfAccounts.objects.filter(pk=accounts[number].pk).update(
                name=name + INACTIVE_SUFFIX)

    # 3. Repoint payment methods BEFORE moving money, so a sale booked
    #    mid-migration cannot land in an account that is about to be drained.
    for source_number, destination_number in CONSOLIDATION.items():
        PaymentMethod.objects.filter(
            linked_account__account_number=source_number
        ).update(linked_account=accounts[destination_number].pk)

    # 4. Move each non-zero balance as a real, reversible transfer with its two
    #    ledger legs. Mirrors journal_engine.post_account_transfer.
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
            # The journal run only sweeps 'unposted' transfers; writing the legs
            # here and marking it posted is what prevents a double-post.
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
        # F() rather than read-modify-write: a concurrent POS write must not be lost.
        ChartOfAccounts.objects.filter(pk=source.pk).update(balance=F('balance') - amount)
        ChartOfAccounts.objects.filter(pk=destination.pk).update(balance=F('balance') + amount)

    # 5. Assert, so a mismatch rolls the whole migration transaction back.
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

    # Strip the suffix first so payment methods can be matched back by name.
    for number in CONSOLIDATION:
        account = ChartOfAccounts.objects.filter(account_number=number).first()
        if account is not None and account.name.endswith(INACTIVE_SUFFIX):
            original = account.name[:-len(INACTIVE_SUFFIX)]
            ChartOfAccounts.objects.filter(pk=account.pk).update(name=original)
            # Each drained account had exactly one same-named payment method.
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
