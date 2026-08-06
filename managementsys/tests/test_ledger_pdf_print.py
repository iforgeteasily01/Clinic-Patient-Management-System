"""Ledger print endpoint — Feature C of
``docs/DESIGN_expense_redesign_and_coa_print.md`` §4.

Covers ``AccountLedgerPrintView`` and ``services.ledger_pdf.build_ledger_pdf``:
that a real PDF comes back, that the guardrails (invalid option combination,
oversized range, unknown enum) fail loudly instead of quietly printing the
wrong document, that an incomplete journal is flagged in red on the page, and
that the two-pass page numbering actually agrees with the page count.

The assertions read the rendered PDF rather than trusting the call not to
raise: reportlab will happily produce a syntactically valid, visually broken
document, and the whole point of this endpoint is what lands on the paper.
"""
import base64
import datetime
import re
import zlib
from decimal import Decimal

import pytest
from django.urls import reverse
from rest_framework.test import APIRequestFactory, force_authenticate

from managementsys.models import JournalDayLog, LedgerEntry
from managementsys.views.admin_views import MAX_LEDGER_PRINT_DAYS, AccountLedgerPrintView
from .factories import AppUserFactory, ChartOfAccountsFactory

START = datetime.date(2026, 8, 1)
END = datetime.date(2026, 8, 31)


# ── PDF introspection ────────────────────────────────────────────────────────

def pdf_text(pdf: bytes) -> bytes:
    """Every text-bearing byte of the document.

    reportlab writes page content streams through ``/ASCII85Decode`` +
    ``/FlateDecode``, so the drawn strings are not visible in the raw file.
    Undo both (each independently optional — short documents skip one) and
    concatenate, so a test can simply ask "does this string appear anywhere".
    """
    chunks = [pdf]
    for raw in re.findall(rb'stream\r?\n(.*?)endstream', pdf, re.S):
        blob = raw.strip()
        try:
            blob = base64.a85decode(blob, adobe=True)
        except Exception:
            pass
        try:
            blob = zlib.decompress(blob)
        except Exception:
            pass
        chunks.append(blob)
    return b'\n'.join(chunks)


def page_labels(pdf: bytes):
    """The 'Hal. X dari Y' footers as a sorted list of (page, total)."""
    return sorted({
        (int(a), int(b))
        for a, b in re.findall(rb'Hal\. (\d+) dari (\d+)', pdf_text(pdf))
    })


def page_count(pdf: bytes) -> int:
    """Page count straight from the PDF's own /Pages node — the independent
    number the footers have to agree with."""
    counts = re.findall(rb'/Count (\d+)', pdf)
    assert counts, 'PDF has no /Pages /Count node'
    return max(int(c) for c in counts)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _entries(account, days, per_day=1, start=START):
    """`days` consecutive dates carrying `per_day` alternating entries each."""
    rows = []
    for d in range(days):
        when = start + datetime.timedelta(days=d)
        for i in range(per_day):
            rows.append(LedgerEntry(
                account=account, date=when,
                description=f'Transaksi {d}-{i}',
                entry_type='debit' if i % 2 == 0 else 'credit',
                amount=Decimal(100_000 + d * 1_000 + i),
                source_type='manual',
            ))
    LedgerEntry.objects.bulk_create(rows)


@pytest.fixture
def account(db):
    return ChartOfAccountsFactory(
        account_number=1100200, name='Bank BCA', account_type='asset',
    )


@pytest.fixture
def ledger(account):
    """Ten days of activity, four entries a day."""
    _entries(account, days=10, per_day=4)
    return account


def _post_days(date_from, date_to):
    """Mark every day in the range journalled. Without this, *every* date in a
    range counts as unposted (a day with no JournalDayLog row was never swept),
    so the warning is the default state, not the exception."""
    cur = date_from
    while cur <= date_to:
        JournalDayLog.objects.create(date=cur, is_posted=True, transaction_count=1)
        cur += datetime.timedelta(days=1)


def _print(pk, **params):
    params.setdefault('date_from', START.isoformat())
    params.setdefault('date_to', END.isoformat())
    request = APIRequestFactory().get('/x/', params)
    force_authenticate(request, user=AppUserFactory(pin='654321'))
    return AccountLedgerPrintView.as_view()(request, pk=pk)


def _body(response):
    return b''.join(response.streaming_content) if response.streaming else response.content


# ── Happy path ───────────────────────────────────────────────────────────────

def test_route_is_registered():
    assert reverse('admin-account-ledger-print', args=[7]) == '/api/admin/accounts/7/ledger/print/'


def test_returns_a_real_pdf(ledger):
    res = _print(ledger.pk)
    assert res.status_code == 200
    assert res['Content-Type'] == 'application/pdf'

    pdf = _body(res)
    assert pdf[:4] == b'%PDF'                      # not an HTML error page
    assert pdf.rstrip()[-5:] == b'%%EOF'           # …and it is complete
    assert len(pdf) > 2_000, 'suspiciously small for a 40-row ledger'
    assert page_count(pdf) >= 1


def test_content_disposition_is_inline_with_a_named_file(ledger):
    res = _print(ledger.pk)
    assert res['Content-Disposition'] == (
        f'inline; filename="buku-besar-1100200-{START}-{END}.pdf"'
    )
    # inline, never attachment — the user opens it in a tab and hits Ctrl+P
    assert 'attachment' not in res['Content-Disposition']


def test_header_block_carries_the_account_and_the_user_supplied_text(ledger):
    text = pdf_text(_body(_print(
        ledger.pk, title='Rekap Kas Agustus', subtitle='Diperiksa oleh manajer',
    )))
    assert b'Rekap Kas Agustus' in text
    assert b'Diperiksa oleh manajer' in text
    assert b'Bank BCA' in text          # account NAME, first
    assert b'1100200' in text           # account number


def test_grouping_per_day_with_page_breaks_gives_one_page_per_day(ledger):
    """Ten days of entries, a fresh page each — the sanity check §6 asks for
    manually, done automatically."""
    res = _print(ledger.pk, group_by='day', page_break='group')
    assert res.status_code == 200
    pdf = _body(res)
    assert page_count(pdf) == 10
    assert b'01 Agustus 2026' in pdf_text(pdf)


def test_empty_range_still_renders(account):
    res = _print(account.pk)
    assert res.status_code == 200
    assert _body(res)[:4] == b'%PDF'


def test_unknown_account_is_404(db):
    assert _print(999_999).status_code == 404


# ── Two-pass page numbering ──────────────────────────────────────────────────

def test_page_numbering_is_stable_across_the_two_pass_build(account):
    """Every page is stamped, the pages run 1..N with no gaps, and the total
    printed in the footer matches the PDF's own page count.

    A footer cannot know the page total while its page is being laid out; the
    NumberedCanvas buffers pages and only stamps them in save(). If that
    buffering breaks, the symptom is a document that says 'Hal. 3 dari 1'.
    """
    _entries(account, days=60, per_day=4)
    pdf = _body(_print(account.pk))

    total = page_count(pdf)
    assert total > 1, 'need a multi-page document for this to mean anything'

    labels = page_labels(pdf)
    assert labels, 'no page footer was stamped at all'
    assert {t for _, t in labels} == {total}, 'footers disagree on the page total'
    assert [p for p, _ in labels] == list(range(1, total + 1))


def test_page_numbering_stable_when_grouped(account):
    _entries(account, days=40, per_day=3)
    pdf = _body(_print(account.pk, group_by='day', page_break='group'))
    total = page_count(pdf)
    labels = page_labels(pdf)
    assert {t for _, t in labels} == {total}
    assert [p for p, _ in labels] == list(range(1, total + 1))


# ── Unposted-day warning ─────────────────────────────────────────────────────

def test_unposted_dates_are_flagged_in_the_header(ledger):
    """The range is 31 days; only the first 10 are journalled, so 21 are not."""
    _post_days(START, START + datetime.timedelta(days=9))
    text = pdf_text(_body(_print(ledger.pk)))
    assert b'Perhatian' in text
    assert b'21 tanggal' in text


def test_no_warning_when_every_day_is_posted(ledger):
    _post_days(START, END)
    assert b'Perhatian' not in pdf_text(_body(_print(ledger.pk)))


def test_warning_counts_a_missing_day_log_as_unposted(ledger):
    """A date with no JournalDayLog row at all has never been swept — it must
    count as unposted, not as 'nothing happened, so fine'."""
    _post_days(START, END)
    JournalDayLog.objects.filter(date=datetime.date(2026, 8, 15)).delete()
    text = pdf_text(_body(_print(ledger.pk)))
    assert b'Perhatian' in text
    assert b'1 tanggal' in text


# ── Validation ───────────────────────────────────────────────────────────────

def test_page_break_group_without_grouping_is_rejected(ledger):
    res = _print(ledger.pk, page_break='group', group_by='none')
    assert res.status_code == 400
    assert 'group_by' in res.data['error']


def test_page_break_group_without_grouping_is_rejected_by_default(ledger):
    """group_by defaults to 'none', so omitting it entirely must fail too."""
    res = _print(ledger.pk, page_break='group')
    assert res.status_code == 400


def test_range_longer_than_366_days_is_rejected(ledger):
    long_end = START + datetime.timedelta(days=MAX_LEDGER_PRINT_DAYS)  # 367 days inclusive
    res = _print(ledger.pk, date_to=long_end.isoformat())
    assert res.status_code == 400
    assert '367' in res.data['error']
    assert str(MAX_LEDGER_PRINT_DAYS) in res.data['error']


def test_exactly_366_days_is_allowed(ledger):
    edge = START + datetime.timedelta(days=MAX_LEDGER_PRINT_DAYS - 1)
    res = _print(ledger.pk, date_to=edge.isoformat())
    assert res.status_code == 200


@pytest.mark.parametrize('params', [
    {'entry_type': 'both'},
    {'group_by': 'week'},
    {'page_break': 'always'},
    {'orientation': 'square'},
    {'show_running': 'maybe'},
])
def test_unknown_option_values_are_rejected_never_silently_defaulted(ledger, params):
    res = _print(ledger.pk, **params)
    assert res.status_code == 400
    assert 'error' in res.data


def test_both_dates_are_required(ledger):
    for params in ({'date_from': ''}, {'date_to': ''}):
        res = _print(ledger.pk, **params)
        assert res.status_code == 400


def test_malformed_date_is_rejected(ledger):
    res = _print(ledger.pk, date_from='01-08-2026')
    assert res.status_code == 400
    assert 'YYYY-MM-DD' in res.data['error']


def test_reversed_range_is_rejected(ledger):
    res = _print(ledger.pk, date_from=END.isoformat(), date_to=START.isoformat())
    assert res.status_code == 400


# ── Options actually change the document ─────────────────────────────────────

def test_landscape_is_wider_than_portrait(ledger):
    portrait = _body(_print(ledger.pk))
    land = _body(_print(ledger.pk, orientation='landscape'))
    assert b'/MediaBox [ 0 0 595.2756 841.8898 ]' in portrait
    assert b'/MediaBox [ 0 0 841.8898 595.2756 ]' in land


def test_show_opening_toggles_the_saldo_awal_row(ledger):
    assert b'Saldo Awal' in pdf_text(_body(_print(ledger.pk)))
    assert b'Saldo Awal' not in pdf_text(_body(_print(ledger.pk, show_opening='false')))


def test_show_subtotals_toggles_the_per_group_line(ledger):
    grouped = pdf_text(_body(_print(ledger.pk, group_by='day')))
    assert b'Subtotal' in grouped
    assert b'Subtotal' not in pdf_text(
        _body(_print(ledger.pk, group_by='day', show_subtotals='0'))
    )


def test_entry_type_filter_is_labelled_on_the_page(ledger):
    """A filtered ledger that does not say it is filtered is a trap."""
    assert b'Hanya debit' in pdf_text(_body(_print(ledger.pk, entry_type='debit')))
    assert b'Semua transaksi' in pdf_text(_body(_print(ledger.pk)))


# ── Formatting primitives ────────────────────────────────────────────────────
# Every number on every printed report goes through these, so a regression here
# is silent and total.

@pytest.mark.parametrize('value,expected', [
    (Decimal('1234567.891'), '1.234.567,89'),
    (Decimal('-1234567.5'), '-1.234.567,50'),
    (Decimal('0'), '0,00'),
    (Decimal('999'), '999,00'),
    (None, '—'),
])
def test_indonesian_number_formatting(value, expected):
    from managementsys.services.ledger_pdf import format_amount
    assert format_amount(value) == expected


def test_month_names_do_not_depend_on_the_server_locale():
    from managementsys.services import ledger_pdf as lp
    assert lp.format_date_long(datetime.date(2026, 8, 5)) == '05 Agustus 2026'
    assert lp.format_month_long(datetime.date(2026, 12, 1)) == 'Desember 2026'
    assert len(lp.MONTHS_ID) == 12 and len(lp.MONTHS_ID_SHORT) == 12


def test_ref_column_surfaces_whichever_identifier_the_row_has():
    from types import SimpleNamespace

    from managementsys.services.ledger_pdf import ledger_ref

    blank = dict(invoice=None, purchase_invoice=None, transfer=None, expense=None)
    assert ledger_ref(SimpleNamespace(**{**blank, 'invoice': SimpleNamespace(pk=1, invoice_number='INV-2201')})) == 'INV-2201'
    assert ledger_ref(SimpleNamespace(**{**blank, 'purchase_invoice': SimpleNamespace(pk=2, internal_id='PUR-9')})) == 'PUR-9'
    assert ledger_ref(SimpleNamespace(**{**blank, 'transfer': SimpleNamespace(pk=3)})) == 'TRF-3'
    assert ledger_ref(SimpleNamespace(**{**blank, 'expense': SimpleNamespace(pk=4)})) == 'EXP-4'
    assert ledger_ref(SimpleNamespace(**blank)) == ''


def test_clinic_name_comes_from_site_config(db):
    """Branding source is the SiteConfig singleton behind the receipt settings,
    not a hardcoded string — the constant is only the empty-install fallback."""
    from managementsys.models import SiteConfig
    from managementsys.services.ledger_pdf import DEFAULT_CLINIC_NAME, resolve_clinic_name

    assert resolve_clinic_name() == DEFAULT_CLINIC_NAME
    SiteConfig.objects.create(pk=1, clinic_name='Medya Clinic')
    assert resolve_clinic_name() == 'Medya Clinic'


def test_clinic_name_is_printed_on_the_document(ledger):
    from managementsys.models import SiteConfig
    SiteConfig.objects.create(pk=1, clinic_name='Medya Clinic')
    assert b'Medya Clinic' in pdf_text(_body(_print(ledger.pk)))
