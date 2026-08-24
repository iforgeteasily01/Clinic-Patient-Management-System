"""Bank reconciliation: matching, the arithmetic, and the clearing stamp.

Three properties these tests exist to defend:

  * **Auto-matching never guesses between distinguishable records.** One
    statement line with two candidate book rows is left unmatched: those rows
    are different real transactions, and picking one misattributes the
    clearing. The reverse — two identical statement lines and one book row — is
    not the same situation and is handled differently; see
    ``test_one_book_row_clears_only_one_of_two_identical_lines``.
  * **A transaction is cleared once.** Completing a period stamps its ledger
    rows, and the next period must not offer them again.
  * **A reconciliation that does not balance cannot be closed.** A closed
    reconciliation with a difference looks finished, so nobody returns to it.
"""
import datetime
import io
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import (
    BankReconciliation, BankStatementLine, ChartOfAccounts, LedgerEntry,
)
from managementsys.services import bank_reconciliation as recon
from managementsys.services import statement_import

from .factories import ChartOfAccountsFactory

D = Decimal
JAN = datetime.date(2026, 1, 1)


@pytest.fixture
def bank(db):
    """A real cash/bank account — inside the 11xxxxx band, so it is selectable."""
    head = ChartOfAccountsFactory(account_number=1100000, name='Kas & Bank',
                                  account_type='asset', is_head=True)
    return ChartOfAccountsFactory(account_number=1100200, name='Bank BCA',
                                  account_type='asset', is_head=False, parent=head)


def _entry(bank, day, amount, description='', entry_type=None):
    """One ledger row on the bank account. Positive amount = money in."""
    signed_in = amount > 0
    return LedgerEntry.objects.create(
        account=bank,
        date=JAN + datetime.timedelta(days=day),
        description=description or f'Transaksi {amount}',
        entry_type=entry_type or ('debit' if signed_in else 'credit'),
        amount=abs(amount),
        source_type='manual',
    )


@pytest.fixture
def rec(bank):
    return BankReconciliation.objects.create(
        account=bank,
        statement_start=JAN,
        statement_end=JAN + datetime.timedelta(days=30),
        opening_balance=D('0'),
        closing_balance=D('0'),
    )


def _line(rec, day, amount, description=''):
    return BankStatementLine.objects.create(
        reconciliation=rec,
        date=JAN + datetime.timedelta(days=day),
        description=description or f'Statement {amount}',
        amount=D(str(amount)),
    )


# ── Sign normalisation ────────────────────────────────────────────────────────

def test_a_debit_on_an_asset_account_is_money_in(bank):
    entry = _entry(bank, 1, D('500000'))
    assert recon.signed_amount(entry) == D('500000')


def test_a_credit_on_an_asset_account_is_money_out(bank):
    entry = _entry(bank, 1, D('-250000'))
    assert recon.signed_amount(entry) == D('-250000')


# ── Auto-matching ─────────────────────────────────────────────────────────────

def test_same_date_same_amount_matches(rec, bank):
    entry = _entry(bank, 3, D('500000'))
    line = _line(rec, 3, '500000')

    assert recon.auto_match(rec) == 1

    line.refresh_from_db()
    assert line.ledger_entry_id == entry.pk
    assert line.match_type == BankStatementLine.MATCH_AUTO


def test_a_few_days_of_settlement_delay_still_matches(rec, bank):
    entry = _entry(bank, 3, D('500000'))
    line = _line(rec, 5, '500000')

    assert recon.auto_match(rec) == 1
    line.refresh_from_db()
    assert line.ledger_entry_id == entry.pk


def test_beyond_the_window_the_amount_alone_is_not_enough(rec, bank):
    _entry(bank, 1, D('500000'))
    line = _line(rec, 20, '500000')

    assert recon.auto_match(rec) == 0
    line.refresh_from_db()
    assert line.ledger_entry_id is None


def test_two_identical_amounts_on_one_day_are_left_for_a_human(rec, bank):
    """The load-bearing refusal. Pairing these arbitrarily would balance the
    period against a transaction that may have happened once."""
    _entry(bank, 3, D('500000'), 'Setoran A')
    _entry(bank, 3, D('500000'), 'Setoran B')
    line = _line(rec, 3, '500000')

    assert recon.auto_match(rec) == 0
    line.refresh_from_db()
    assert line.ledger_entry_id is None


def test_one_book_row_clears_only_one_of_two_identical_lines(rec, bank):
    """The mirror image of the test above, and deliberately not symmetric.

    Two *book rows* competing for one statement line is refused, because those
    rows are distinct records — 'Setoran A' and 'Setoran B' are different real
    transactions and picking one misattributes the clearing.

    Two identical *statement lines* competing for one book row is different:
    the lines are indistinguishable from each other, so which one gets matched
    carries no information. Matching one and leaving the other is the more
    useful answer — it says "one Rp 500.000 deposit is missing from your books"
    as a single unmatched line, instead of leaving three loose ends that mean
    the same thing.
    """
    _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    _line(rec, 3, '500000')

    recon.auto_match(rec)

    assert rec.lines.filter(ledger_entry__isnull=False).count() == 1
    assert rec.lines.filter(ledger_entry__isnull=True).count() == 1
    # And the leftover is exactly the size of the discrepancy.
    assert recon.summary(rec)['unmatched_line_total'] == D('500000')


def test_exact_date_wins_over_a_nearby_one(rec, bank):
    near = _entry(bank, 2, D('500000'), 'Dua hari lebih awal')
    exact = _entry(bank, 4, D('500000'), 'Tanggal sama')
    line = _line(rec, 4, '500000')

    recon.auto_match(rec)

    line.refresh_from_db()
    assert line.ledger_entry_id == exact.pk
    assert line.ledger_entry_id != near.pk


def test_direction_matters_not_just_magnitude(rec, bank):
    """A Rp 500.000 payment out must never match a Rp 500.000 deposit in."""
    _entry(bank, 3, D('-500000'), 'Pembayaran keluar')
    line = _line(rec, 3, '500000')

    assert recon.auto_match(rec) == 0
    line.refresh_from_db()
    assert line.ledger_entry_id is None


def test_an_ignored_line_is_not_auto_matched(rec, bank):
    _entry(bank, 3, D('500000'))
    line = _line(rec, 3, '500000')
    line.is_ignored = True
    line.save()

    assert recon.auto_match(rec) == 0


# ── Manual matching ───────────────────────────────────────────────────────────

def test_manual_match_can_force_what_the_matcher_refused(rec, bank):
    a = _entry(bank, 3, D('500000'), 'Setoran A')
    _entry(bank, 3, D('500000'), 'Setoran B')
    line = _line(rec, 3, '500000')

    recon.match_line(rec, line, a)

    line.refresh_from_db()
    assert line.ledger_entry_id == a.pk
    assert line.match_type == BankStatementLine.MATCH_MANUAL


def test_a_row_from_another_account_cannot_be_matched(rec, bank):
    other = ChartOfAccountsFactory(account_number=1100300, name='Bank Lain',
                                   account_type='asset')
    foreign = LedgerEntry.objects.create(
        account=other, date=JAN, description='x', entry_type='debit',
        amount=D('500000'), source_type='manual',
    )
    line = _line(rec, 3, '500000')

    with pytest.raises(recon.ReconciliationError):
        recon.match_line(rec, line, foreign)


def test_the_same_book_row_cannot_clear_two_lines_by_hand(rec, bank):
    entry = _entry(bank, 3, D('500000'))
    first = _line(rec, 3, '500000')
    second = _line(rec, 4, '500000')

    recon.match_line(rec, first, entry)

    with pytest.raises(recon.ReconciliationError):
        recon.match_line(rec, second, entry)


def test_matching_is_refused_on_a_completed_reconciliation(rec, bank):
    entry = _entry(bank, 3, D('500000'))
    line = _line(rec, 3, '500000')
    recon.match_line(rec, line, entry)
    rec.closing_balance = D('500000')
    rec.save()
    recon.complete(rec, None)

    other = _entry(bank, 4, D('100000'))
    with pytest.raises(recon.ReconciliationError):
        recon.match_line(rec, line, other)


# ── The arithmetic ────────────────────────────────────────────────────────────

def test_a_clean_period_reports_no_difference(rec, bank):
    _entry(bank, 3, D('500000'))
    _entry(bank, 5, D('-200000'))
    _line(rec, 3, '500000')
    _line(rec, 5, '-200000')
    rec.closing_balance = D('300000')
    rec.save()
    recon.auto_match(rec)

    figures = recon.summary(rec)

    assert figures['book_balance'] == D('300000')
    assert figures['difference'] == D('0')
    assert figures['is_balanced'] is True
    assert figures['can_complete'] is True


def test_a_bank_charge_nobody_recorded_shows_as_an_unmatched_line(rec, bank):
    _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    _line(rec, 6, '-15000', 'Biaya admin')
    rec.closing_balance = D('485000')
    rec.save()
    recon.auto_match(rec)

    figures = recon.summary(rec)

    assert figures['unmatched_line_count'] == 1
    assert figures['unmatched_line_total'] == D('-15000')
    assert figures['difference'] == D('15000')
    assert figures['can_complete'] is False


def test_a_cheque_that_has_not_cleared_shows_as_an_unmatched_book_entry(rec, bank):
    _entry(bank, 3, D('500000'))
    _entry(bank, 28, D('-100000'), 'Cek belum cair')
    _line(rec, 3, '500000')
    rec.closing_balance = D('500000')
    rec.save()
    recon.auto_match(rec)

    figures = recon.summary(rec)

    assert figures['unmatched_entry_count'] == 1
    assert figures['unmatched_entry_total'] == D('-100000')
    assert figures['difference'] == D('-100000')


def test_statement_drift_is_reported_separately_from_the_difference(rec, bank):
    """An incomplete import is its own finding: chasing the main difference
    before fixing it is wasted effort."""
    _line(rec, 3, '500000')
    rec.opening_balance = D('0')
    rec.closing_balance = D('700000')   # the bank says more than the lines add to
    rec.save()

    figures = recon.summary(rec)

    assert figures['statement_total'] == D('500000')
    assert figures['statement_computed_closing'] == D('500000')
    assert figures['statement_drift'] == D('200000')


def test_an_ignored_line_stops_counting_against_the_reconciliation(rec, bank):
    _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    stray = _line(rec, 6, '-15000', 'Bukan milik rekening ini')
    rec.closing_balance = D('500000')
    rec.save()
    recon.auto_match(rec)

    assert recon.summary(rec)['can_complete'] is False

    stray.is_ignored = True
    stray.save()

    assert recon.summary(rec)['can_complete'] is True


# ── Completing and clearing ───────────────────────────────────────────────────

def test_completing_is_refused_while_a_difference_remains(rec, bank):
    _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    rec.closing_balance = D('400000')
    rec.save()
    recon.auto_match(rec)

    with pytest.raises(recon.ReconciliationError) as exc:
        recon.complete(rec, None)

    assert 'selisih' in str(exc.value.errors).lower()


def test_completing_stamps_every_matched_row(rec, bank):
    entry = _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    rec.closing_balance = D('500000')
    rec.save()
    recon.auto_match(rec)

    recon.complete(rec, None)

    entry.refresh_from_db()
    rec.refresh_from_db()
    assert entry.reconciliation_id == rec.pk
    assert rec.status == 'completed'


def test_a_cleared_row_is_not_offered_to_the_next_period(rec, bank):
    entry = _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    rec.closing_balance = D('500000')
    rec.save()
    recon.auto_match(rec)
    recon.complete(rec, None)

    # A second, overlapping period. The already-cleared row must not reappear.
    later = BankReconciliation.objects.create(
        account=bank,
        statement_start=JAN,
        statement_end=JAN + datetime.timedelta(days=40),
        opening_balance=D('0'), closing_balance=D('0'),
    )

    assert entry.pk not in {e.pk for e in recon.book_entries(later)}


def test_reopening_releases_the_cleared_rows(rec, bank):
    entry = _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    rec.closing_balance = D('500000')
    rec.save()
    recon.auto_match(rec)
    recon.complete(rec, None)

    recon.reopen(rec)

    entry.refresh_from_db()
    rec.refresh_from_db()
    assert entry.reconciliation_id is None
    assert rec.status == 'draft'
    # The matches survive, so the operator resumes rather than restarting.
    assert rec.lines.filter(ledger_entry__isnull=False).count() == 1


def test_reconciliation_never_writes_to_the_ledger(rec, bank):
    """The whole feature is an assertion about the books, not a posting path."""
    _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    rec.closing_balance = D('500000')
    rec.save()

    before = LedgerEntry.objects.count()
    recon.auto_match(rec)
    recon.complete(rec, None)

    assert LedgerEntry.objects.count() == before


# ── Statement import ──────────────────────────────────────────────────────────

def _upload(text, name='statement.csv'):
    buf = io.BytesIO(text.encode('utf-8'))
    buf.name = name
    return buf


def test_separate_debit_and_credit_columns_become_one_signed_number():
    result = statement_import.parse(_upload(
        'Tanggal,Keterangan,Debit,Kredit\n'
        '05/01/2026,Setoran tunai,,500000\n'
        '06/01/2026,Biaya admin,15000,\n'
    ))

    rows = [r for r in result['rows'] if r['ok']]
    assert [r['amount'] for r in rows] == ['500000', '-15000']


def test_indonesian_thousand_separators_are_read_correctly():
    result = statement_import.parse(_upload(
        'Tanggal,Keterangan,Debit,Kredit\n'
        '05/01/2026,Setoran,,"1.234.567,89"\n'
    ))

    assert result['rows'][0]['amount'] == '1234567.89'


def test_english_separators_are_read_correctly():
    result = statement_import.parse(_upload(
        'Date,Description,Debit,Credit\n'
        '2026-01-05,Deposit,,"1,234,567.89"\n'
    ))

    assert result['rows'][0]['amount'] == '1234567.89'


def test_a_direction_column_signs_a_single_amount_column():
    result = statement_import.parse(_upload(
        'Tanggal,Keterangan,Jumlah,DK\n'
        '05/01/2026,Setoran,500000,CR\n'
        '06/01/2026,Tarik tunai,200000,DB\n'
    ))

    rows = [r for r in result['rows'] if r['ok']]
    assert [r['amount'] for r in rows] == ['500000', '-200000']


def test_an_unsigned_amount_with_no_direction_is_refused_not_guessed():
    """Guessing here silently reverses half a statement."""
    result = statement_import.parse(_upload(
        'Tanggal,Keterangan,Jumlah\n'
        '05/01/2026,Setoran,500000\n'
    ))

    assert result['rows'][0]['ok'] is False
    assert 'tidak bertanda' in result['rows'][0]['problem']


def test_the_ambiguous_k_marker_is_refused():
    """'K' is kredit in some exports and keluar in others."""
    result = statement_import.parse(_upload(
        'Tanggal,Keterangan,Jumlah,DK\n'
        '05/01/2026,Setoran,500000,K\n'
    ))

    assert result['rows'][0]['ok'] is False
    assert 'ambigu' in result['rows'][0]['problem']


def test_preamble_rows_above_the_header_are_skipped():
    result = statement_import.parse(_upload(
        'REKENING KORAN\n'
        'No. Rekening: 1234567890\n'
        'Periode: Januari 2026\n'
        '\n'
        'Tanggal,Keterangan,Debit,Kredit\n'
        '05/01/2026,Setoran,,500000\n'
    ))

    assert result['summary']['usable_rows'] == 1


def test_rows_outside_the_period_are_flagged_not_dropped(rec):
    """An operator who picked the wrong file finds out from the preview."""
    result = statement_import.parse(
        _upload('Tanggal,Keterangan,Debit,Kredit\n'
                '05/01/2026,Dalam periode,,500000\n'
                '05/06/2026,Luar periode,,100000\n'),
        date_from=rec.statement_start, date_to=rec.statement_end,
    )

    outside = [r for r in result['rows'] if r['out_of_period']]
    assert len(outside) == 1
    assert outside[0]['ok'] is False


def test_a_file_with_no_recognisable_header_is_rejected_as_a_whole():
    with pytest.raises(statement_import.StatementParseError):
        statement_import.parse(_upload('foo,bar,baz\n1,2,3\n'))


# ── API surface ───────────────────────────────────────────────────────────────

def test_a_non_cash_account_cannot_be_reconciled(auth_api, bank, db):
    inventory = ChartOfAccountsFactory(account_number=1300000, name='Persediaan',
                                       account_type='asset')

    res = auth_api.post(reverse('reconciliations'), {
        'account': inventory.id,
        'statement_start': '2026-01-01',
        'statement_end': '2026-01-31',
    }, format='json')

    assert res.status_code == 400


def test_overlapping_periods_on_one_account_are_refused(auth_api, bank):
    payload = {
        'account': bank.id,
        'statement_start': '2026-01-01',
        'statement_end': '2026-01-31',
        'opening_balance': '0',
        'closing_balance': '0',
    }
    assert auth_api.post(reverse('reconciliations'), payload, format='json').status_code == 201

    overlap = {**payload, 'statement_start': '2026-01-15', 'statement_end': '2026-02-15'}
    res = auth_api.post(reverse('reconciliations'), overlap, format='json')

    assert res.status_code == 400
    assert 'tumpang tindih' in str(res.json())


def test_the_workspace_returns_lines_entries_and_summary_together(auth_api, rec, bank):
    _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')

    res = auth_api.get(reverse('reconciliation-workspace', args=[rec.pk]))

    assert res.status_code == 200
    body = res.json()
    assert len(body['lines']) == 1
    assert len(body['book_entries']) == 1
    assert 'difference' in body['summary']


def test_import_confirm_writes_the_rows_and_matches_what_it_can(auth_api, rec, bank):
    _entry(bank, 3, D('500000'))

    res = auth_api.post(
        reverse('reconciliation-import-confirm', args=[rec.pk]),
        {'rows': [{'date': (JAN + datetime.timedelta(days=3)).isoformat(),
                   'description': 'Setoran', 'amount': '500000'}]},
        format='json',
    )

    assert res.status_code == 201
    assert res.json()['imported'] == 1
    assert res.json()['auto_matched'] == 1


def test_reimporting_with_replace_does_not_double_the_lines(auth_api, rec, bank):
    row = {'date': (JAN + datetime.timedelta(days=3)).isoformat(),
           'description': 'Setoran', 'amount': '500000'}
    url = reverse('reconciliation-import-confirm', args=[rec.pk])

    auth_api.post(url, {'rows': [row]}, format='json')
    auth_api.post(url, {'rows': [row], 'replace': True}, format='json')

    assert rec.lines.count() == 1


def test_ignoring_a_matched_line_also_unmatches_it(auth_api, rec, bank):
    entry = _entry(bank, 3, D('500000'))
    line = _line(rec, 3, '500000')
    recon.match_line(rec, line, entry)

    res = auth_api.post(
        reverse('reconciliation-line-action', args=[rec.pk, line.pk, 'ignore']),
        {'ignored': True}, format='json',
    )

    assert res.status_code == 200
    line.refresh_from_db()
    assert line.is_ignored is True
    assert line.ledger_entry_id is None


def test_a_completed_reconciliation_cannot_be_deleted(auth_api, rec, bank):
    _entry(bank, 3, D('500000'))
    _line(rec, 3, '500000')
    rec.closing_balance = D('500000')
    rec.save()
    recon.auto_match(rec)
    recon.complete(rec, None)

    res = auth_api.delete(reverse('reconciliation-detail', args=[rec.pk]))

    assert res.status_code == 400
