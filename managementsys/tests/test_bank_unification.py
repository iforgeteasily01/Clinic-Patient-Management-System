"""Migration 0089 — BCA/Mandiri account unification.

Builds the pre-migration COA shape (six per-instrument asset accounts, six
payment methods, some prior ledger history), runs the migration's forward
function against the live app registry, and checks that the money moved without
disturbing anything already in the books.
"""
import importlib
from datetime import date
from decimal import Decimal

import pytest
from django.apps import apps as global_apps

from managementsys.accounting_checks import trial_balance
from managementsys.models import (
    AccountTransfer,
    ChartOfAccounts,
    LedgerEntry,
    PaymentMethod,
)

from .factories import ChartOfAccountsFactory, PaymentMethodFactory

# Module name starts with a digit, so it cannot be a plain import statement.
migration = importlib.import_module(
    'managementsys.migrations.0089_unify_bank_accounts'
)


# account_number -> (name, starting balance)
INSTRUMENTS = {
    1100002: ('Transfer BCA',   Decimal('15114009.07')),
    1100003: ('Debit BCA',      Decimal('183458200.00')),
    1100004: ('Kredit BCA',     Decimal('79345400.00')),
    1100005: ('QRIS Mandiri',   Decimal('238144000.00')),
    1100006: ('Debit Mandiri',  Decimal('17175000.00')),
    1100007: ('Kredit Mandiri', Decimal('38883560.00')),
}
HISTORY_DATE = date(2026, 7, 15)
BCA_TOTAL = sum(INSTRUMENTS[n][1] for n in (1100002, 1100003, 1100004))
MANDIRI_TOTAL = sum(INSTRUMENTS[n][1] for n in (1100005, 1100006, 1100007))


@pytest.fixture
def bank_coa(db):
    """The pre-0089 cash group: head, six instrument accounts, six methods.

    Each account also carries one prior debit ledger row equal to its balance,
    standing in for the sales history that must not be touched.
    """
    head = ChartOfAccountsFactory(
        account_number=1100000, name='Cash & Payment Accounts',
        account_type='asset', is_head=True, is_system=True,
    )
    revenue = ChartOfAccountsFactory(
        account_number=4200000, name='Sales Revenue', account_type='revenue',
    )
    accounts, methods, history = {}, {}, {}
    for number, (name, balance) in INSTRUMENTS.items():
        account = ChartOfAccountsFactory(
            account_number=number, name=name, account_type='asset',
            balance=balance, is_head=False, parent=head,
        )
        accounts[number] = account
        methods[number] = PaymentMethodFactory(name=name, linked_account=account)
        history[number] = LedgerEntry.objects.create(
            account=account, date=HISTORY_DATE, description=f'Penjualan {name}',
            entry_type='debit', amount=balance, source_type='invoice',
        )
        # Balancing revenue leg, so the fixture ledger starts balanced.
        LedgerEntry.objects.create(
            account=revenue, date=HISTORY_DATE, description=f'Penjualan {name}',
            entry_type='credit', amount=balance, source_type='invoice',
        )
    return {'head': head, 'accounts': accounts, 'methods': methods, 'history': history}


def run_forward():
    migration.unify_bank_accounts(global_apps, None)


def run_reverse():
    migration.restore_bank_accounts(global_apps, None)


@pytest.mark.django_db
def test_sources_drain_and_destinations_absorb(bank_coa):
    run_forward()

    bca = ChartOfAccounts.objects.get(account_number=1100002)
    mandiri = ChartOfAccounts.objects.get(account_number=1100005)
    assert bca.name == 'Bank BCA'
    assert mandiri.name == 'Bank Mandiri'
    assert bca.balance == BCA_TOTAL
    assert mandiri.balance == MANDIRI_TOTAL

    for number in (1100003, 1100004, 1100006, 1100007):
        drained = ChartOfAccounts.objects.get(account_number=number)
        assert drained.balance == Decimal('0')
        assert drained.name.endswith(' (nonaktif)')


@pytest.mark.django_db
def test_all_six_methods_resolve_to_two_accounts(bank_coa):
    run_forward()

    resolved = {
        m.name: m.linked_account.account_number
        for m in PaymentMethod.objects.filter(
            name__in=[name for name, _ in INSTRUMENTS.values()]
        )
    }
    assert resolved == {
        'Transfer BCA': 1100002, 'Debit BCA': 1100002, 'Kredit BCA': 1100002,
        'QRIS Mandiri': 1100005, 'Debit Mandiri': 1100005, 'Kredit Mandiri': 1100005,
    }
    # Names and rows survive — only the GL account they settle to changed.
    assert PaymentMethod.objects.filter(
        name__in=[name for name, _ in INSTRUMENTS.values()]
    ).count() == 6


@pytest.mark.django_db
def test_trial_balance_unchanged(bank_coa):
    dr_before, cr_before = trial_balance()
    run_forward()
    dr_after, cr_after = trial_balance()

    moved = BCA_TOTAL - INSTRUMENTS[1100002][1] + MANDIRI_TOTAL - INSTRUMENTS[1100005][1]
    # Two-legged transfers: debits and credits grow by the same amount, so the
    # books stay balanced even though both totals move.
    assert dr_after - dr_before == moved
    assert cr_after - cr_before == moved
    assert dr_after == cr_after


@pytest.mark.django_db
def test_historic_ledger_rows_untouched(bank_coa):
    before = {
        number: (entry.account_id, entry.amount, entry.entry_type, entry.date)
        for number, entry in bank_coa['history'].items()
    }
    run_forward()

    for number, entry in bank_coa['history'].items():
        entry.refresh_from_db()
        assert (entry.account_id, entry.amount, entry.entry_type, entry.date) == before[number]


@pytest.mark.django_db
def test_transfers_are_posted_and_referenced(bank_coa):
    run_forward()

    transfers = AccountTransfer.objects.filter(reference__startswith='MIGRASI-0089')
    assert transfers.count() == 4
    for transfer in transfers:
        assert transfer.posting_status == 'posted'
        assert transfer.reference == f'MIGRASI-0089-{transfer.from_account.account_number}'
        legs = LedgerEntry.objects.filter(transfer=transfer)
        assert legs.count() == 2
        assert {leg.entry_type for leg in legs} == {'debit', 'credit'}
        assert all(leg.source_type == 'transfer' for leg in legs)
        assert all(leg.amount == transfer.amount for leg in legs)


@pytest.mark.django_db
def test_zero_balance_source_writes_no_transfer(bank_coa):
    ChartOfAccounts.objects.filter(account_number=1100004).update(balance=Decimal('0'))
    LedgerEntry.objects.filter(account__account_number=1100004).delete()

    run_forward()

    assert not AccountTransfer.objects.filter(reference='MIGRASI-0089-1100004').exists()
    assert AccountTransfer.objects.filter(reference__startswith='MIGRASI-0089').count() == 3
    assert ChartOfAccounts.objects.get(account_number=1100002).balance == (
        INSTRUMENTS[1100002][1] + INSTRUMENTS[1100003][1]
    )


@pytest.mark.django_db
def test_second_run_is_a_no_op(bank_coa):
    run_forward()
    snapshot = {
        a.account_number: (a.name, a.balance)
        for a in ChartOfAccounts.objects.filter(account_number__in=INSTRUMENTS)
    }
    ledger_count = LedgerEntry.objects.count()

    run_forward()

    assert {
        a.account_number: (a.name, a.balance)
        for a in ChartOfAccounts.objects.filter(account_number__in=INSTRUMENTS)
    } == snapshot
    assert LedgerEntry.objects.count() == ledger_count
    assert AccountTransfer.objects.filter(reference__startswith='MIGRASI-0089').count() == 4


@pytest.mark.django_db
def test_reverse_restores_the_original_shape(bank_coa):
    run_forward()
    run_reverse()

    for number, (name, balance) in INSTRUMENTS.items():
        account = ChartOfAccounts.objects.get(account_number=number)
        assert account.name == name
        assert account.balance == balance
        assert PaymentMethod.objects.get(name=name).linked_account_id == account.pk

    assert not AccountTransfer.objects.filter(reference__startswith='MIGRASI-0089').exists()
    assert not LedgerEntry.objects.filter(source_type='transfer').exists()
