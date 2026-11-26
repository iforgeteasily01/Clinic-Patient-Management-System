"""Kartu stok: GET /api/inventory/items/<pk>/movements/.

The history is reconstructed from source documents, so the property worth
defending is that it **agrees with the batches**: after a sale, its journal run,
a return and a stock-out, the running balance the card shows must equal what the
FIFO batches actually hold. A row in the wrong state (counted while still
pending, or not counted once posted) breaks that equality.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import (
    InventoryBatch, Invoice, JournalEntry, LedgerEntry, Patient, PurchaseInvoice, Supplier,
)

from .factories import InventoryItemFactory, WarehouseFactory


@pytest.fixture
def manager_api(auth_api, auth_user):
    auth_user.role = 'manager'
    auth_user.save(update_fields=['role'])
    return auth_api


@pytest.fixture
def patient(db):
    return Patient.objects.create(patient_no='S000001', name='Siti Aminah')


def _card(api, item, **params):
    res = api.get(reverse('inventory-item-movements', args=[item.pk]), params)
    assert res.status_code == 200, res.content
    return res.json()


def _run_journal(api):
    res = api.post(reverse('accounting-journal-run'),
                   {'date_to': datetime.date.today().isoformat()}, format='json')
    assert res.status_code == 200, res.content


def _sell(api, stock, gl_accounts, *, quantity=4, patient=None):
    body = {
        'warehouse_id': stock['warehouse'].id,
        'payment_method_id': gl_accounts['cash_method'].id,
        'grand_total': str(10000 * quantity),
        'items': [{'item_id': stock['item'].id, 'price': '10000', 'quantity': quantity}],
    }
    if patient:
        body['patient_no'] = patient.patient_no
    res = api.post(reverse('invoice-create'), body, format='json')
    assert res.status_code == 201, res.content
    return Invoice.objects.get(pk=res.json()['id'])


def test_requires_manager_or_superuser(auth_api, stock):
    # auth_user is a cashier: the rows carry patient names.
    res = auth_api.get(reverse('inventory-item-movements', args=[stock['item'].pk]))
    assert res.status_code == 403


def test_sale_names_the_patient_and_links_the_invoice(manager_api, stock, gl_accounts, patient):
    invoice = _sell(manager_api, stock, gl_accounts, patient=patient)

    row = next(r for r in _card(manager_api, stock['item'])['results'] if r['kind'] == 'sale')
    assert row['document'] == {'type': 'invoice', 'id': invoice.id,
                               'number': invoice.invoice_number}
    assert row['patient'] == {'patient_no': 'S000001', 'name': 'Siti Aminah'}
    assert Decimal(row['quantity_out']) == Decimal('4')


def test_a_sale_is_pending_until_the_journal_run_moves_the_stock(manager_api, stock, gl_accounts):
    _sell(manager_api, stock, gl_accounts)

    card = _card(manager_api, stock['item'])
    sale = next(r for r in card['results'] if r['kind'] == 'sale')
    assert sale['status'] == 'pending'
    assert sale['balance_after'] is None
    assert Decimal(card['history_balance']) == Decimal('100')
    assert card['totals']['pending_count'] == 1

    _run_journal(manager_api)

    card = _card(manager_api, stock['item'])
    sale = next(r for r in card['results'] if r['kind'] == 'sale')
    assert sale['status'] == 'applied'
    assert Decimal(sale['balance_after']) == Decimal('96')


def test_the_card_agrees_with_the_batches_after_a_full_cycle(manager_api, stock, gl_accounts, patient):
    invoice = _sell(manager_api, stock, gl_accounts, patient=patient)
    _run_journal(manager_api)

    res = manager_api.post(reverse('sales-returns'), {
        'invoice': invoice.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': invoice.items.first().id, 'quantity': 1}],
    }, format='json')
    assert res.status_code == 201, res.content
    _run_journal(manager_api)

    res = manager_api.post('/api/inventory/stock-out/', {
        'item_id': stock['item'].id, 'warehouse_id': stock['warehouse'].id,
        'quantity': 3, 'reason': 'expired',
    }, format='json')
    assert res.status_code == 200, res.content

    card = _card(manager_api, stock['item'])
    kinds = [r['kind'] for r in card['results']]
    assert kinds == ['stock_out', 'sale_return', 'sale', 'stock_in']   # newest first
    assert all(r['status'] == 'applied' for r in card['results'])
    assert Decimal(card['on_hand']) == Decimal('94')
    assert Decimal(card['history_balance']) == Decimal('94')
    assert Decimal(card['discrepancy']) == 0

    rtn = card['results'][1]
    assert rtn['patient']['name'] == 'Siti Aminah'
    assert rtn['document']['type'] == 'sales_return'


def test_excluded_and_voided_sales(manager_api, stock, gl_accounts):
    excluded = _sell(manager_api, stock, gl_accounts, quantity=2)
    Invoice.objects.filter(pk=excluded.pk).update(posting_status='excluded')
    voided = _sell(manager_api, stock, gl_accounts, quantity=5)
    Invoice.objects.filter(pk=voided.pk).update(is_voided=True)

    card = _card(manager_api, stock['item'])
    sales = [r for r in card['results'] if r['kind'] == 'sale']
    assert [r['document']['id'] for r in sales] == [excluded.id]
    assert sales[0]['status'] == 'not_deducted'
    assert Decimal(card['history_balance']) == Decimal('100')


def test_pencacahan_moves_both_items_and_its_batch_is_not_a_stock_in(manager_api, stock):
    target = InventoryItemFactory(unit_small='ml')
    res = manager_api.post('/api/inventory/pencacahan/', {
        'source_item_id': stock['item'].id, 'source_warehouse_id': stock['warehouse'].id,
        'source_quantity': 2,
        'target_item_id': target.id, 'target_warehouse_id': stock['warehouse'].id,
        'target_quantity': 100,
    }, format='json')
    assert res.status_code in (200, 201), res.content

    src = _card(manager_api, stock['item'])
    assert src['results'][0]['kind'] == 'pencacahan_out'
    assert Decimal(src['history_balance']) == Decimal('98')

    tgt = _card(manager_api, target)
    assert [r['kind'] for r in tgt['results']] == ['pencacahan_in']
    assert Decimal(tgt['discrepancy']) == 0


def test_a_purchase_split_by_an_edit_is_one_row(manager_api):
    item = InventoryItemFactory()
    wh = WarehouseFactory()
    pi = PurchaseInvoice.objects.create(
        supplier=Supplier.objects.create(name='PT Sumber'),
        purchase_date=datetime.date(2026, 11, 2), external_invoice_no='F-77',
    )
    # What an edit after a sale leaves behind: a fully-drawn frozen batch plus
    # the on-hand remainder, both stamped with the same purchase.
    for initial, remaining in ((Decimal('4'), Decimal('0')), (Decimal('6'), Decimal('6'))):
        InventoryBatch.objects.create(
            item=item, warehouse=wh, input_date=pi.purchase_date,
            quantity_initial=initial, quantity_remaining=remaining,
            value=initial * 1000, purchase_invoice=pi,
        )

    rows = _card(manager_api, item)['results']
    assert len(rows) == 1
    assert rows[0]['kind'] == 'purchase'
    assert Decimal(rows[0]['quantity_in']) == Decimal('10')
    assert rows[0]['counterparty'] == 'PT Sumber'
    assert rows[0]['document'] == {'type': 'purchase_invoice', 'id': pi.id,
                                   'number': pi.internal_id}


def test_date_window_carries_an_opening_balance(manager_api, stock):
    wh = stock['warehouse']
    InventoryBatch.objects.create(
        item=stock['item'], warehouse=wh, input_date=datetime.date(2020, 1, 1),
        quantity_initial=Decimal('7'), quantity_remaining=Decimal('7'), value=0,
    )
    card = _card(manager_api, stock['item'], date_from=datetime.date.today().isoformat())

    assert Decimal(card['opening_balance']) == Decimal('7')
    assert [r['kind'] for r in card['results']] == ['stock_in']
    # The balance on a row is the same whatever window is asked for.
    assert Decimal(card['results'][0]['balance_after']) == Decimal('107')


def test_bad_parameters_are_rejected(manager_api, stock):
    url = reverse('inventory-item-movements', args=[stock['item'].pk])
    assert manager_api.get(url, {'date_from': '24-11-2026'}).status_code == 400
    assert manager_api.get(url, {'kind': 'teleport'}).status_code == 400


def _imported_sale(stock, number, when, quantity):
    inv = Invoice.objects.create(
        invoice_number=number, datetime=when, warehouse=stock['warehouse'],
        grand_total=Decimal('10000') * quantity, posting_status='posted',
    )
    inv.items.create(item=stock['item'], item_name=stock['item'].name,
                     quantity=quantity, price=Decimal('10000'))
    return inv


def test_imported_ipos_sales_are_listed_but_do_not_count(manager_api, stock):
    """History brought over from iPos predates the opening stock batch, which
    was keyed in already net of it — counting it would sink the balance."""
    when = datetime.datetime(2024, 5, 1, 3, 0, tzinfo=datetime.timezone.utc)
    _imported_sale(stock, 'IPOS-1/KSR/GD/0524', when, 30)

    card = _card(manager_api, stock['item'])
    sale = next(r for r in card['results'] if r['kind'] == 'sale')
    assert sale['status'] == 'not_deducted'
    assert sale['date'] == '2024-05-01'
    assert Decimal(card['history_balance']) == Decimal('100')


def test_an_imported_sale_a_journal_run_did_draw_on_counts_on_the_draw_date(
        manager_api, stock, gl_accounts):
    """Some imports were swept later and took real stock. The ledger row is the
    evidence; the row is dated when the stock moved, not the 2024 invoice date."""
    when = datetime.datetime(2024, 5, 1, 3, 0, tzinfo=datetime.timezone.utc)
    inv = _imported_sale(stock, 'IPOS-2/KSR/GD/0524', when, 30)
    swept_at = datetime.datetime(2026, 8, 6, 8, 2, tzinfo=datetime.timezone.utc)
    je = JournalEntry.objects.create(entry_number='JE-TEST-1', date=when.date(), invoice=inv)
    JournalEntry.objects.filter(pk=je.pk).update(created_at=swept_at)   # auto_now_add
    LedgerEntry.objects.create(
        account=gl_accounts['inventory_asset'], date=when.date(), entry_type='credit',
        amount=Decimal('150000'), invoice=inv, journal_entry=je,
        description=f'Invoice {inv.invoice_number} – FIFO deduction: {stock["item"].name}',
    )
    InventoryBatch.objects.filter(pk=stock['batch'].pk).update(quantity_remaining=Decimal('70'))

    card = _card(manager_api, stock['item'])
    sale = next(r for r in card['results'] if r['kind'] == 'sale')
    assert sale['status'] == 'applied'
    assert sale['date'] == '2026-08-06'
    assert '01-05-2024' in sale['note']
    assert Decimal(card['history_balance']) == Decimal('70')
    assert Decimal(card['discrepancy']) == 0
