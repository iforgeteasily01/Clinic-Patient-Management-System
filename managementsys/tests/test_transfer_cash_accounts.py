"""Transfers move money between cash/bank accounts, and only between those.

A transfer is a move of funds between two places money physically sits. Moving
value anywhere else — writing down inventory, reclassifying an expense — is a
manual adjustment or a correction journal, and must not be expressible as a
transfer. Posting stays deferred: the two legs land when the journal run sweeps
the transfer date, not at creation.
"""
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import AccountTransfer, LedgerEntry


def _post_transfer(auth_api, from_acct, to_acct, *, amount=5000, date='2026-08-01'):
    return auth_api.post(reverse('accounting-transfers'), {
        'transfer_date': date,
        'from_account': from_acct.id,
        'to_account': to_acct.id,
        'amount': amount,
        'description': 'test transfer',
    }, format='json')


@pytest.mark.django_db
class TestTransferAccountsMustBeCash:
    def test_cash_to_cash_is_accepted(self, auth_api, gl_accounts):
        res = _post_transfer(auth_api, gl_accounts['cash'], gl_accounts['bank'])
        assert res.status_code == 201, res.content

    def test_source_outside_the_cash_set_is_rejected(self, auth_api, gl_accounts):
        res = _post_transfer(auth_api, gl_accounts['inventory_asset'], gl_accounts['cash'])
        assert res.status_code == 400, res.content
        assert 'kas/bank' in res.json()['error']
        assert not AccountTransfer.objects.exists()

    def test_destination_outside_the_cash_set_is_rejected(self, auth_api, gl_accounts):
        res = _post_transfer(auth_api, gl_accounts['cash'], gl_accounts['tax_payable'])
        assert res.status_code == 400, res.content
        assert 'kas/bank' in res.json()['error']
        assert not AccountTransfer.objects.exists()


@pytest.mark.django_db
class TestTransferPostsOnJournalRun:
    def test_legs_land_only_once_the_run_sweeps_the_date(self, auth_api, gl_accounts):
        assert _post_transfer(auth_api, gl_accounts['cash'], gl_accounts['bank'],
                              amount=5000, date='2026-08-01').status_code == 201
        transfer = AccountTransfer.objects.latest('id')

        # Deferred: nothing in the ledger, no balance moved.
        assert transfer.posting_status == 'unposted'
        assert LedgerEntry.objects.filter(transfer=transfer).count() == 0

        res = auth_api.post(reverse('accounting-journal-run'),
                            {'date_to': '2026-08-01'}, format='json')
        assert res.status_code == 200, res.content

        transfer.refresh_from_db()
        assert transfer.posting_status == 'posted'

        legs = {(e.account_id, e.entry_type): e.amount
                for e in LedgerEntry.objects.filter(transfer=transfer)}
        assert legs == {
            (gl_accounts['cash'].id, 'credit'): Decimal('5000'),
            (gl_accounts['bank'].id, 'debit'): Decimal('5000'),
        }

        gl_accounts['cash'].refresh_from_db()
        gl_accounts['bank'].refresh_from_db()
        assert gl_accounts['cash'].balance == Decimal('-5000')
        assert gl_accounts['bank'].balance == Decimal('5000')
