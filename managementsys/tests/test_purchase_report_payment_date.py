"""The purchase report carries each invoice's settlement date.

A purchase is settled in instalments, each PurchasePayment with its own date,
so there is no single payment date on PurchaseInvoice. The report files an
invoice under its *newest* payment, and an invoice with no payment rows reports
an empty date rather than guessing at the purchase date.
"""
import datetime
import io

import openpyxl
import pytest
from django.urls import reverse

from managementsys.models import (
    PurchaseInvoice, PurchaseInvoiceItem, PurchasePayment, Supplier,
)

from .factories import InventoryItemFactory, WarehouseFactory

pytestmark = pytest.mark.django_db


def _invoice(supplier, warehouse, item, *, internal_id, purchase_date, pay_dates):
    inv = PurchaseInvoice.objects.create(
        internal_id=internal_id,
        supplier=supplier,
        warehouse=warehouse,
        purchase_date=purchase_date,
        total_amount=100000,
        amount_paid=100000 if pay_dates else 0,
        status='paid' if pay_dates else 'unpaid',
    )
    PurchaseInvoiceItem.objects.create(
        invoice=inv, item=item, item_name=item.name,
        quantity=1, unit='pcs', unit_cost=100000, actual_unit_cost=100000,
    )
    for d in pay_dates:
        PurchasePayment.objects.create(invoice=inv, payment_date=d, amount=50000)
    return inv


@pytest.fixture
def purchases(db):
    supplier  = Supplier.objects.create(name='PT Contoh')
    warehouse = WarehouseFactory(code='G1')
    item      = InventoryItemFactory()
    # Two instalments, deliberately out of chronological insert order so a
    # "last row wins" implementation would pick the wrong one.
    paid = _invoice(
        supplier, warehouse, item,
        internal_id='PI-PAID', purchase_date=datetime.date(2026, 11, 1),
        pay_dates=[datetime.date(2026, 11, 10), datetime.date(2026, 11, 4)],
    )
    unpaid = _invoice(
        supplier, warehouse, item,
        internal_id='PI-UNPAID', purchase_date=datetime.date(2026, 11, 3),
        pay_dates=[],
    )
    return paid, unpaid


def _fetch(auth_api, fmt):
    return auth_api.get(
        reverse('reports-generate'),
        {'type': 'purchases', 'start': '2026-11-01', 'end': '2026-11-30', 'format': fmt},
    )


def test_json_report_carries_last_payment_date(auth_api, purchases):
    res = _fetch(auth_api, 'json')
    assert res.status_code == 200, res.content
    headers = {g['header']['No Transaksi']: g['header'] for g in res.json()['groups']}

    # The newest of the two instalments, not the most recently inserted row.
    assert headers['PI-PAID']['Tgl. Bayar'] == '2026-11-10'
    # Never falls back to the purchase date.
    assert headers['PI-UNPAID']['Tgl. Bayar'] == ''
    assert headers['PI-UNPAID']['Tanggal'] == '2026-11-03'


def test_xlsx_banner_shows_payment_date(auth_api, purchases):
    res = _fetch(auth_api, 'xlsx')
    assert res.status_code == 200, res.content

    ws = openpyxl.load_workbook(io.BytesIO(res.content)).active
    banners = [
        str(c.value) for row in ws.iter_rows() for c in row
        if c.value and 'No Transaksi' in str(c.value)
    ]
    paid   = next(b for b in banners if 'PI-PAID' in b)
    unpaid = next(b for b in banners if 'PI-UNPAID' in b)

    assert 'Tgl. Bayar: 2026-11-10' in paid
    assert 'Tgl. Bayar: -' in unpaid
