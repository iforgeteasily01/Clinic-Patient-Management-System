"""Manual journal entries: double entry, transaction classification, open period.

Covers:
  * the catalog is internally consistent (unique codes, every rule explains itself)
  * classification names the common shapes, prefers the more specific rule, and
    reports an unrecognised combination as unknown rather than guessing
  * the balancing hint reports which side is short and by how much
  * the posting window is the open period only — at or before the last journal
    run is refused, so is the future
  * a posted entry gets a JournalEntry header, its lines attached to it, and
    every balance rolled in its account's natural direction (the bug the retired
    single-sided endpoint had: it moved credit-normal accounts the wrong way)
  * an entry that does not balance, has one side only, or targets a head account
    is refused
  * the retired /adjustments/ endpoint answers 410
"""
import datetime
from decimal import Decimal

import pytest
from django.db.models import Max
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    AuditLog, ChartOfAccounts, JournalDayLog, JournalEntry, LedgerEntry,
)
from managementsys.services import manual_journal as mj

from .factories import ChartOfAccountsFactory


def _today():
    """The app's "today" — Django's active timezone, not the OS clock."""
    return timezone.localdate()


def _post(auth_api, **kwargs):
    payload = {'date': _today().isoformat(), 'memo': 'Uji entri manual', **kwargs}
    return auth_api.post(reverse('accounting-manual-journal'), payload, format='json')


def _line(account, entry_type, amount, description=''):
    return {
        'account': account.id, 'entry_type': entry_type,
        'amount': str(amount), 'description': description,
    }


# ── Catalog ───────────────────────────────────────────────────────────────────

class TestCatalog:
    def test_codes_are_unique(self):
        codes = [t.code for t in mj.TRANSACTION_TYPES]
        assert len(codes) == len(set(codes))

    def test_every_rule_explains_itself(self):
        for t in mj.TRANSACTION_TYPES:
            assert t.name and t.what and t.example, t.code
            assert t.debit and t.credit, t.code

    def test_every_rule_uses_known_account_types(self):
        for t in mj.TRANSACTION_TYPES:
            for side in (t.debit, t.credit):
                assert side <= set(mj.TYPE_LABELS), t.code

    def test_named_bands_have_labels(self):
        """A band with no label renders as '?' in the shape string."""
        for t in mj.TRANSACTION_TYPES:
            for band in t.debit_bands + t.credit_bands:
                assert band in mj.BAND_LABELS, t.code

    def test_shape_names_the_family_when_the_rule_requires_one(self):
        sale = mj.TYPES_BY_CODE['sale']
        assert 'Kas / Bank' in sale.shape
        assert 'Pendapatan' in sale.shape


# ── Classification ────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestClassification:
    def test_cash_and_revenue_is_a_sale(self, gl_accounts):
        result = mj.classify([
            (gl_accounts['cash'], 'debit', Decimal('100')),
            (gl_accounts['revenue'], 'credit', Decimal('100')),
        ])
        assert result['code'] == 'sale'
        assert result['confidence'] == 'exact'

    def test_cogs_and_inventory_is_cogs_recognition(self, gl_accounts):
        result = mj.classify([
            (gl_accounts['cogs'], 'debit', Decimal('50')),
            (gl_accounts['inventory_asset'], 'credit', Decimal('50')),
        ])
        assert result['code'] == 'cogs_recognise'
        assert result['caution']          # warns that FIFO already posts product COGS

    def test_cash_to_cash_is_a_transfer_not_a_generic_asset_move(self, gl_accounts):
        """Both sides land in the cash band, so the transfer rule outranks the
        type-only asset/asset rules."""
        result = mj.classify([
            (gl_accounts['bank'], 'debit', Decimal('500')),
            (gl_accounts['cash'], 'credit', Decimal('500')),
        ])
        assert result['code'] == 'asset_transfer'

    def test_tax_payment_beats_generic_liability_payment(self, gl_accounts):
        result = mj.classify([
            (gl_accounts['tax_payable'], 'debit', Decimal('4400')),
            (gl_accounts['cash'], 'credit', Decimal('4400')),
        ])
        assert result['code'] == 'tax_pay'

    def test_generic_liability_payment_when_no_family_matches(self, gl_accounts):
        other_liability = ChartOfAccountsFactory(
            account_number=2400000, name='Utang Gaji', account_type='liability',
        )
        result = mj.classify([
            (other_liability, 'debit', Decimal('1000')),
            (gl_accounts['cash'], 'credit', Decimal('1000')),
        ])
        assert result['code'] == 'liability_pay'

    def test_unrecognised_combination_is_unknown_not_a_guess(self, gl_accounts):
        result = mj.classify([
            (gl_accounts['cogs'], 'debit', Decimal('10')),
            (gl_accounts['revenue'], 'credit', Decimal('10')),
        ])
        assert result['confidence'] == 'unknown'
        assert result['code'] == ''

    def test_blank_rows_do_not_affect_classification(self, gl_accounts):
        result = mj.classify([
            (gl_accounts['cash'], 'debit', Decimal('100')),
            (gl_accounts['revenue'], 'credit', Decimal('100')),
            (None, 'debit', Decimal('0')),
            (gl_accounts['cogs'], 'debit', Decimal('0')),
        ])
        assert result['code'] == 'sale'

    def test_one_sided_draft_classifies_as_nothing(self, gl_accounts):
        result = mj.classify([(gl_accounts['cash'], 'debit', Decimal('100'))])
        assert result['confidence'] == 'unknown'

    def test_compound_entry_headlines_the_largest_component(self, gl_accounts):
        """Cash sale plus an accrued expense in one entry. Neither single rule
        covers both sides, so the pairs are reported and the biggest leads."""
        expense = ChartOfAccountsFactory(
            account_number=6100000, name='Beban Gaji', account_type='expense',
        )
        liability = ChartOfAccountsFactory(
            account_number=2400001, name='Utang Gaji', account_type='liability',
        )
        result = mj.classify([
            (gl_accounts['cash'], 'debit', Decimal('1000')),
            (expense, 'debit', Decimal('10')),
            (gl_accounts['revenue'], 'credit', Decimal('1000')),
            (liability, 'credit', Decimal('10')),
        ])
        assert result['confidence'] == 'compound'
        assert result['code'] == 'sale'
        assert result['alternatives']


# ── Balancing ─────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestBalanceHint:
    def test_reports_the_short_side_and_amount(self, gl_accounts):
        hint = mj.balance_hint([
            (gl_accounts['cash'], 'debit', Decimal('1000')),
            (gl_accounts['revenue'], 'credit', Decimal('400')),
        ])
        assert hint['needs_side'] == 'credit'
        assert hint['needs_amount'] == Decimal('600')

    def test_reports_debit_short(self, gl_accounts):
        hint = mj.balance_hint([
            (gl_accounts['revenue'], 'credit', Decimal('900')),
        ])
        assert hint['needs_side'] == 'debit'
        assert hint['needs_amount'] == Decimal('900')

    def test_balanced_needs_nothing(self, gl_accounts):
        hint = mj.balance_hint([
            (gl_accounts['cash'], 'debit', Decimal('700')),
            (gl_accounts['revenue'], 'credit', Decimal('700')),
        ])
        assert hint['needs_side'] == ''
        assert hint['difference'] == 0


# ── Posting window ────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestPostingWindow:
    def test_no_run_yet_leaves_the_past_open(self):
        today = _today()
        earliest, latest, blocked = mj.posting_window(today)
        assert earliest is None
        assert latest == today
        assert blocked is False
        assert mj.validate_date(today - datetime.timedelta(days=30), today) is None

    def test_future_is_always_refused(self):
        today = _today()
        err = mj.validate_date(today + datetime.timedelta(days=1), today)
        assert err and 'melewati hari ini' in err

    def test_dates_at_or_before_the_last_run_are_refused(self):
        today = _today()
        run_date = today - datetime.timedelta(days=5)
        JournalDayLog.objects.create(date=run_date, is_posted=True)

        assert mj.last_run_date() == run_date
        assert mj.validate_date(run_date, today) is not None
        assert mj.validate_date(run_date - datetime.timedelta(days=1), today) is not None
        assert mj.validate_date(run_date + datetime.timedelta(days=1), today) is None
        assert mj.validate_date(today, today) is None

    def test_a_day_skipped_by_an_earlier_sweep_stays_closed(self):
        """The guard is the *last* run date, not per-day. A gap before it belongs
        to a period already reported on."""
        today = _today()
        JournalDayLog.objects.create(date=today - datetime.timedelta(days=10), is_posted=True)
        JournalDayLog.objects.create(date=today - datetime.timedelta(days=3), is_posted=True)
        skipped = today - datetime.timedelta(days=7)
        assert mj.validate_date(skipped, today) is not None

    def test_unposted_day_logs_do_not_close_anything(self):
        today = _today()
        JournalDayLog.objects.create(date=today - datetime.timedelta(days=2), is_posted=False)
        assert mj.last_run_date() is None

    def test_run_through_today_blocks_every_date(self):
        today = _today()
        JournalDayLog.objects.create(date=today, is_posted=True)
        _earliest, _latest, blocked = mj.posting_window(today)
        assert blocked is True
        assert mj.validate_date(today, today) == mj.BLOCKED_REASON


# ── Meta endpoint ─────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestMetaEndpoint:
    def test_serves_window_and_catalog(self, auth_api):
        res = auth_api.get(reverse('accounting-manual-journal-meta'))
        assert res.status_code == 200, res.content
        body = res.json()
        assert body['window']['blocked'] is False
        assert len(body['transaction_types']) == len(mj.TRANSACTION_TYPES)
        assert {'code', 'name', 'shape', 'what', 'example'} <= set(body['transaction_types'][0])

    def test_window_reports_the_last_run(self, auth_api):
        run_date = _today() - datetime.timedelta(days=4)
        JournalDayLog.objects.create(date=run_date, is_posted=True)
        body = auth_api.get(reverse('accounting-manual-journal-meta')).json()
        assert body['window']['last_run_date'] == run_date.isoformat()
        assert body['window']['earliest'] == (run_date + datetime.timedelta(days=1)).isoformat()


# ── Classify endpoint ─────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestClassifyEndpoint:
    def test_returns_balance_and_name_without_writing(self, auth_api, gl_accounts):
        res = auth_api.post(
            reverse('accounting-manual-journal-classify'),
            {'lines': [
                _line(gl_accounts['cash'], 'debit', 1000),
                _line(gl_accounts['revenue'], 'credit', 600),
            ]},
            format='json',
        )
        assert res.status_code == 200, res.content
        body = res.json()
        assert body['balance']['is_balanced'] is False
        assert body['balance']['needs_side'] == 'credit'
        assert body['balance']['needs_amount'] == '400.00'
        assert body['classification']['code'] == 'sale'
        assert LedgerEntry.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_empty_draft_is_not_an_error(self, auth_api):
        res = auth_api.post(reverse('accounting-manual-journal-classify'),
                            {'lines': []}, format='json')
        assert res.status_code == 200, res.content
        assert res.json()['classification']['confidence'] == 'unknown'


# ── Posting ───────────────────────────────────────────────────────────────────

@pytest.mark.django_db
class TestManualJournalPosting:
    def test_balanced_entry_posts_with_a_header(self, auth_api, gl_accounts):
        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 1000, 'Terima tunai'),
            _line(gl_accounts['revenue'], 'credit', 1000, 'Penjualan'),
        ])
        assert res.status_code == 201, res.content
        body = res.json()

        entry = JournalEntry.objects.get(pk=body['entry']['id'])
        assert entry.source_type == 'adjustment'
        assert entry.date == _today()
        assert entry.is_balanced is True
        assert entry.total_debit == entry.total_credit == Decimal('1000.00')
        assert entry.entry_number.startswith(f'JE-{_today().year}-')

        # Every line belongs to the header — the invariant the ledger relies on.
        assert entry.lines.count() == 2
        assert LedgerEntry.objects.filter(journal_entry__isnull=True).count() == 0
        assert all(l.source_type == 'adjustment' and l.date == _today()
                   for l in entry.lines.all())

        assert body['classification']['code'] == 'sale'

    def test_balances_move_in_each_account_natural_direction(self, auth_api, gl_accounts):
        """The retired single-sided endpoint did +amount for a debit and -amount
        for a credit regardless of type, so a credit to revenue moved it the
        wrong way. Both accounts must rise here."""
        cash_before = gl_accounts['cash'].balance
        revenue_before = gl_accounts['revenue'].balance

        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 2500),
            _line(gl_accounts['revenue'], 'credit', 2500),
        ])
        assert res.status_code == 201, res.content

        gl_accounts['cash'].refresh_from_db()
        gl_accounts['revenue'].refresh_from_db()
        assert gl_accounts['cash'].balance == cash_before + Decimal('2500')
        assert gl_accounts['revenue'].balance == revenue_before + Decimal('2500')

    def test_multi_line_entry_posts(self, auth_api, gl_accounts):
        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 600),
            _line(gl_accounts['bank'], 'debit', 400),
            _line(gl_accounts['revenue'], 'credit', 1000),
        ])
        assert res.status_code == 201, res.content
        assert JournalEntry.objects.get().lines.count() == 3

    def test_writes_an_audit_log(self, auth_api, gl_accounts):
        _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
        ])
        log = AuditLog.objects.get(entity_type='JournalEntry')
        assert 'Entri jurnal manual' in log.description

    def test_line_description_falls_back_to_the_memo(self, auth_api, gl_accounts):
        res = _post(auth_api, memo='Setoran modal', lines=[
            _line(gl_accounts['cash'], 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
        ])
        assert res.status_code == 201, res.content
        assert all(l.description == 'Setoran modal'
                   for l in JournalEntry.objects.get().lines.all())

    def test_blank_trailing_rows_are_ignored(self, auth_api, gl_accounts):
        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
            {'account': None, 'entry_type': 'debit', 'amount': '', 'description': ''},
        ])
        assert res.status_code == 201, res.content
        assert JournalEntry.objects.get().lines.count() == 2


@pytest.mark.django_db
class TestManualJournalRefusals:
    def _assert_nothing_written(self):
        assert LedgerEntry.objects.count() == 0
        assert JournalEntry.objects.count() == 0

    def test_unbalanced_entry_is_refused(self, auth_api, gl_accounts):
        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 1000),
            _line(gl_accounts['revenue'], 'credit', 900),
        ])
        assert res.status_code == 400
        assert 'tidak seimbang' in res.json()['error']
        self._assert_nothing_written()

    def test_single_sided_entry_is_refused(self, auth_api, gl_accounts):
        res = _post(auth_api, lines=[_line(gl_accounts['cash'], 'debit', 1000)])
        assert res.status_code == 400
        assert 'minimal dua baris' in res.json()['error']
        self._assert_nothing_written()

    def test_two_lines_on_the_same_side_are_refused(self, auth_api, gl_accounts):
        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 500),
            _line(gl_accounts['bank'], 'debit', 500),
        ])
        assert res.status_code == 400
        assert 'kredit' in res.json()['error']
        self._assert_nothing_written()

    def test_head_account_is_refused(self, auth_api, gl_accounts):
        head = ChartOfAccounts.objects.get(account_number=1100000)
        assert head.is_head
        res = _post(auth_api, lines=[
            _line(head, 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
        ])
        assert res.status_code == 400
        assert 'akun induk' in res.json()['error']
        self._assert_nothing_written()

    def test_zero_amount_line_is_refused(self, auth_api, gl_accounts):
        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
            {'account': gl_accounts['bank'].id, 'entry_type': 'debit', 'amount': '0'},
        ])
        # A row carrying an account but no amount is a mistake, not a blank row.
        assert res.status_code == 400
        self._assert_nothing_written()

    def test_missing_memo_is_refused(self, auth_api, gl_accounts):
        res = _post(auth_api, memo='  ', lines=[
            _line(gl_accounts['cash'], 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
        ])
        assert res.status_code == 400
        assert 'Keterangan' in res.json()['error']
        self._assert_nothing_written()

    def test_closed_period_is_refused(self, auth_api, gl_accounts):
        run_date = _today() - datetime.timedelta(days=3)
        JournalDayLog.objects.create(date=run_date, is_posted=True)

        res = _post(auth_api, date=run_date.isoformat(), lines=[
            _line(gl_accounts['cash'], 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
        ])
        assert res.status_code == 400
        assert 'Jurnal Koreksi' in res.json()['error']
        self._assert_nothing_written()

    def test_open_day_after_the_last_run_is_accepted(self, auth_api, gl_accounts):
        run_date = _today() - datetime.timedelta(days=3)
        JournalDayLog.objects.create(date=run_date, is_posted=True)

        res = _post(auth_api, date=(run_date + datetime.timedelta(days=1)).isoformat(),
                    lines=[
                        _line(gl_accounts['cash'], 'debit', 100),
                        _line(gl_accounts['revenue'], 'credit', 100),
                    ])
        assert res.status_code == 201, res.content

    def test_future_date_is_refused(self, auth_api, gl_accounts):
        res = _post(auth_api, date=(_today() + datetime.timedelta(days=1)).isoformat(),
                    lines=[
                        _line(gl_accounts['cash'], 'debit', 100),
                        _line(gl_accounts['revenue'], 'credit', 100),
                    ])
        assert res.status_code == 400
        self._assert_nothing_written()

    def test_run_through_today_blocks_posting(self, auth_api, gl_accounts):
        JournalDayLog.objects.create(date=_today(), is_posted=True)
        res = _post(auth_api, lines=[
            _line(gl_accounts['cash'], 'debit', 100),
            _line(gl_accounts['revenue'], 'credit', 100),
        ])
        assert res.status_code == 400
        self._assert_nothing_written()

    def test_unknown_account_is_refused(self, auth_api, gl_accounts):
        missing_id = ChartOfAccounts.objects.aggregate(m=Max('id'))['m'] + 1000
        res = _post(auth_api, lines=[
            {'account': missing_id, 'entry_type': 'debit', 'amount': '100'},
            _line(gl_accounts['revenue'], 'credit', 100),
        ])
        assert res.status_code == 400
        self._assert_nothing_written()


@pytest.mark.django_db
class TestRetiredAdjustmentEndpoint:
    def test_single_sided_adjustments_are_gone(self, auth_api, gl_accounts):
        res = auth_api.post(
            reverse('accounting-adjustments'),
            {'account': gl_accounts['cash'].id, 'entry_type': 'debit',
             'amount': '1000', 'date': _today().isoformat(), 'description': 'x'},
            format='json',
        )
        assert res.status_code == 410
        assert 'Entri Jurnal Manual' in res.json()['error']
        assert LedgerEntry.objects.count() == 0
