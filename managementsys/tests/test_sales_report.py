"""Sales report over a date range — summary, per-day rows, invoice list, export.

The three endpoints share ``services/sales_report.py``; the load-bearing
property is that they agree with each other: the per-day rows sum to the total,
the invoice list counts the same invoices, and the workbook carries the same
figures.
"""
import datetime
import io
from decimal import Decimal
from zoneinfo import ZoneInfo

import openpyxl
import pytest
from django.urls import reverse

from managementsys.models import Invoice, InvoicePayment, Patient, SalesReturn

from .factories import AppUserFactory, BranchFactory, PaymentMethodFactory

WIB = ZoneInfo('Asia/Jakarta')


def _wib(y, m, d, hh=10, mm=0):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=WIB)


def _invoice(when, total, **kw):
    return Invoice.objects.create(datetime=when, grand_total=Decimal(total), **kw)


def _report(api, start, end):
    res = api.get(reverse('reports-sales'), {'start': start, 'end': end})
    assert res.status_code == 200, res.content
    return res.json()


@pytest.mark.django_db
class TestSummaryAndDays:
    def test_every_day_is_listed_and_days_sum_to_the_total(self, auth_api):
        _invoice(_wib(2026, 3, 1), '100000')
        _invoice(_wib(2026, 3, 1, 15), '50000')
        _invoice(_wib(2026, 3, 3), '70000')

        data = _report(auth_api, '2026-03-01', '2026-03-04')

        days = {d['date']: d for d in data['by_day']}
        assert list(days) == ['2026-03-01', '2026-03-02', '2026-03-03', '2026-03-04']
        assert Decimal(days['2026-03-01']['total']) == Decimal('150000')
        assert days['2026-03-01']['invoice_count'] == 2
        # A day with no sales is shown as zero, not skipped.
        assert Decimal(days['2026-03-02']['total']) == 0
        assert days['2026-03-02']['invoice_count'] == 0
        assert sum(Decimal(d['total']) for d in data['by_day']) == Decimal(data['total'])
        assert data['invoice_count'] == 3

    def test_days_are_cut_in_jakarta_not_utc(self, auth_api):
        # 23:30 WIB on the 1st is 16:30 UTC on the 1st; 00:30 WIB on the 2nd is
        # 17:30 UTC on the 1st. Cut in UTC, both would land on the 1st.
        _invoice(_wib(2026, 3, 1, 23, 30), '10000')
        _invoice(_wib(2026, 3, 2, 0, 30), '20000')

        days = {d['date']: d for d in _report(auth_api, '2026-03-01', '2026-03-02')['by_day']}

        assert Decimal(days['2026-03-01']['total']) == Decimal('10000')
        assert Decimal(days['2026-03-02']['total']) == Decimal('20000')

    def test_a_single_date_is_a_range_of_one_day(self, auth_api):
        _invoice(_wib(2026, 3, 1), '10000')
        _invoice(_wib(2026, 3, 2), '20000')

        data = _report(auth_api, '2026-03-02', '2026-03-02')

        assert data['total'] == '20000.00'
        assert [d['date'] for d in data['by_day']] == ['2026-03-02']
        assert data['by_day'][0]['weekday'] == 'Senin'

    def test_voided_invoices_are_not_sales(self, auth_api):
        _invoice(_wib(2026, 3, 1), '10000')
        _invoice(_wib(2026, 3, 1), '99000', is_voided=True)

        data = _report(auth_api, '2026-03-01', '2026-03-01')

        assert data['total'] == '10000.00'
        assert data['invoice_count'] == 1

    def test_returns_sit_beside_sales_on_the_day_they_happened(self, auth_api):
        sale = _invoice(_wib(2026, 3, 1), '100000')
        SalesReturn.objects.create(invoice=sale, datetime=_wib(2026, 3, 2),
                                   total_refund=Decimal('30000'))
        SalesReturn.objects.create(invoice=sale, datetime=_wib(2026, 3, 2),
                                   total_refund=Decimal('5000'), is_voided=True)

        data = _report(auth_api, '2026-03-01', '2026-03-02')
        days = {d['date']: d for d in data['by_day']}

        # The sale total is untouched; the return is its own figure.
        assert data['total'] == '100000.00'
        assert Decimal(data['returns_total']) == Decimal('30000')
        assert data['returns_count'] == 1
        assert Decimal(data['net_total']) == Decimal('70000')
        assert Decimal(days['2026-03-01']['returns_total']) == 0
        assert Decimal(days['2026-03-02']['returns_total']) == Decimal('30000')
        assert Decimal(days['2026-03-02']['net_total']) == Decimal('-30000')

    def test_bad_range_is_refused(self, auth_api):
        res = auth_api.get(reverse('reports-sales'), {'start': '2026-03-05', 'end': '2026-03-01'})
        assert res.status_code == 400
        res = auth_api.get(reverse('reports-sales'), {'start': 'kemarin'})
        assert res.status_code == 400


@pytest.mark.django_db
class TestInvoiceList:
    def test_lists_the_same_invoices_the_total_counts_oldest_first(self, auth_api):
        late = _invoice(_wib(2026, 3, 2), '20000')
        early = _invoice(_wib(2026, 3, 1), '10000')
        _invoice(_wib(2026, 3, 1), '5000', is_voided=True)

        res = auth_api.get(reverse('reports-sales-invoices'),
                           {'start': '2026-03-01', 'end': '2026-03-02'})
        body = res.json()

        assert res.status_code == 200, res.content
        assert body['count'] == 2
        assert [r['id'] for r in body['results']] == [early.id, late.id]
        assert body['results'][0]['date'] == '2026-03-01'
        assert body['results'][0]['time'] == '10:00'
        assert body['results'][0]['posting_label'] == 'Belum dijurnal'

    def test_pages_server_side(self, auth_api):
        for i in range(5):
            _invoice(_wib(2026, 3, 1, 8 + i), '1000')

        body = auth_api.get(reverse('reports-sales-invoices'), {
            'start': '2026-03-01', 'end': '2026-03-01', 'page': 2, 'page_size': 2,
        }).json()

        assert body['count'] == 5
        assert body['total_pages'] == 3
        assert [r['time'] for r in body['results']] == ['10:00', '11:00']

    def test_search_by_patient_name_or_invoice_number(self, auth_api):
        pat = Patient.objects.create(name="Siti Aminah")
        mine = _invoice(_wib(2026, 3, 1), '1000', patient_no=pat)
        other = _invoice(_wib(2026, 3, 1), '2000')

        def ids(q):
            return [r['id'] for r in auth_api.get(reverse('reports-sales-invoices'), {
                'start': '2026-03-01', 'end': '2026-03-01', 'q': q,
            }).json()['results']]

        assert ids('aminah') == [mine.id]
        assert ids(other.invoice_number) == [other.id]

    def test_a_split_invoice_names_every_tender(self, auth_api, gl_accounts):
        bank = PaymentMethodFactory(name='QRIS', linked_account=gl_accounts['bank'])
        inv = _invoice(_wib(2026, 3, 1), '350000', payment_method=gl_accounts['cash_method'])
        InvoicePayment.objects.create(invoice=inv, payment_method=gl_accounts['cash_method'],
                                      payment_account=gl_accounts['cash'],
                                      amount=Decimal('200000'), sort_order=0)
        InvoicePayment.objects.create(invoice=inv, payment_method=bank,
                                      payment_account=gl_accounts['bank'],
                                      amount=Decimal('150000'), sort_order=1)

        row = auth_api.get(reverse('reports-sales-invoices'),
                           {'start': '2026-03-01', 'end': '2026-03-01'}).json()['results'][0]

        assert row['is_split'] is True
        assert row['payment'] == 'Cash 200.000 + QRIS 150.000'


@pytest.mark.django_db
class TestBranchScope:
    def test_selected_branch_excludes_the_other_branch(self, api):
        home, other = BranchFactory(), BranchFactory()
        manager = AppUserFactory(role='manager', home_branch=home)
        manager.generate_token()
        api.credentials(HTTP_AUTHORIZATION=f'Bearer {manager.auth_token}')
        _invoice(_wib(2026, 3, 1), '10000', branch=home)
        _invoice(_wib(2026, 3, 1), '20000', branch=other)

        res = api.get(reverse('reports-sales'), {'start': '2026-03-01', 'end': '2026-03-01'},
                      HTTP_X_BRANCH_ID=str(other.pk))
        assert res.json()['total'] == '20000.00'

        res = api.get(reverse('reports-sales-invoices'),
                      {'start': '2026-03-01', 'end': '2026-03-01'},
                      HTTP_X_BRANCH_ID=str(other.pk))
        assert res.json()['count'] == 1


@pytest.mark.django_db
class TestExport:
    def test_requires_authentication(self, api):
        res = api.get(reverse('reports-sales-export'))
        assert res.status_code in (401, 403)

    def test_workbook_carries_the_same_figures(self, auth_api):
        _invoice(_wib(2026, 3, 1), '100000')
        _invoice(_wib(2026, 3, 3), '50000')

        res = auth_api.get(reverse('reports-sales-export'),
                           {'start': '2026-03-01', 'end': '2026-03-03'})

        assert res.status_code == 200, res.content
        assert 'laporan_penjualan_2026-03-01_2026-03-03.xlsx' in res['Content-Disposition']
        wb = openpyxl.load_workbook(io.BytesIO(res.content))
        assert wb.sheetnames == ['Ringkasan', 'Per Hari', 'Faktur', 'Per Item']

        sheet = list(wb['Per Hari'].iter_rows(values_only=True))
        weekdays = ('Minggu', 'Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu')
        per_day = [r for r in sheet if r and r[1] in weekdays]
        totals = [r for r in sheet if r and r[0] == 'TOTAL']
        assert len(per_day) == 3                     # every day, including the empty 2nd
        assert totals[0][5] == 150000

        invoice_rows = [r for r in wb['Faktur'].iter_rows(values_only=True)
                        if r and isinstance(r[0], str) and r[0].startswith('INV-')]
        assert len(invoice_rows) == 2

    def test_single_date_names_the_file_by_that_date(self, auth_api):
        res = auth_api.get(reverse('reports-sales-export'),
                           {'start': '2026-03-01', 'end': '2026-03-01'})
        assert 'laporan_penjualan_2026-03-01.xlsx' in res['Content-Disposition']
