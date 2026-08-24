"""Sales returns: the refund arithmetic, the stock, and the journal.

The invariant these tests defend is that a **full** return of an entire invoice
is value-neutral: sell it, return all of it, run the journal, and every account
balance and every batch is back where it started. If that ever stops holding,
partial returns are wrong too — they just fail less visibly.

Phase 2 applies here exactly as it does to invoices: creating a return writes no
ledger rows and moves no stock. The journal run is what posts it, and the run is
also where the FIFO restock happens, because the COGS the entry needs cannot be
known without doing the restock.
"""
import datetime
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from managementsys.models import (
    ChartOfAccounts, InventoryBatch, Invoice, SalesReturn, SalesReturnItem,
)
from managementsys.services.sales_returns import (
    SalesReturnError, compute_refund, returnable_lines, validate_lines,
)

from .factories import InventoryItemFactory


def _run_journal(auth_api, through=None):
    date_to = (through or datetime.date.today()).isoformat()
    res = auth_api.post(reverse('accounting-journal-run'), {'date_to': date_to}, format='json')
    assert res.status_code == 200, res.content
    return res.json()


def _balances():
    return {a.pk: a.balance for a in ChartOfAccounts.objects.all()}


def _batches():
    return {b.pk: b.quantity_remaining for b in InventoryBatch.objects.all()}


def _sell(auth_api, stock, gl_accounts, *, quantity=4, price=10000,
          discount=0, tax=0, charges=0):
    subtotal = Decimal(price) * Decimal(quantity)
    grand = subtotal - Decimal(discount) + Decimal(tax) + Decimal(charges)
    res = auth_api.post(reverse('invoice-create'), {
        'warehouse_id': stock['warehouse'].id,
        'payment_method_id': gl_accounts['cash_method'].id,
        'discount': str(discount),
        'tax': str(tax),
        'additional_charges': str(charges),
        'grand_total': str(grand),
        'items': [{'item_id': stock['item'].id, 'price': str(price), 'quantity': quantity}],
    }, format='json')
    assert res.status_code == 201, res.content
    return Invoice.objects.get(pk=res.json()['id'])


@pytest.fixture
def sold(auth_api, stock, gl_accounts):
    """One posted invoice: 4 units at 10.000, no discount, stock deducted."""
    invoice = _sell(auth_api, stock, gl_accounts)
    _run_journal(auth_api)
    invoice.refresh_from_db()
    assert invoice.posting_status == 'posted'
    return invoice


# ── What is returnable ────────────────────────────────────────────────────────

def test_returnable_lines_start_fully_open(sold):
    rows = returnable_lines(sold)

    assert len(rows) == 1
    assert Decimal(rows[0]['quantity_open']) == Decimal('4')
    assert Decimal(rows[0]['quantity_returned']) == Decimal('0')
    assert rows[0]['restockable'] is True


def test_a_partial_return_reduces_what_is_still_open(auth_api, sold, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 1}],
    }, format='json')
    assert res.status_code == 201, res.content

    rows = returnable_lines(sold)
    assert Decimal(rows[0]['quantity_returned']) == Decimal('1')
    assert Decimal(rows[0]['quantity_open']) == Decimal('3')


def test_returning_more_than_was_sold_is_refused(auth_api, sold, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 5}],
    }, format='json')

    assert res.status_code == 400
    assert 'melebihi' in str(res.json())


def test_two_partial_returns_cannot_exceed_the_sale_between_them(auth_api, sold, gl_accounts):
    line = sold.items.first()
    payload = {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 3}],
    }
    assert auth_api.post(reverse('sales-returns'), payload, format='json').status_code == 201

    # 3 already back, 4 sold — a second return of 3 must not go through.
    res = auth_api.post(reverse('sales-returns'), payload, format='json')
    assert res.status_code == 400


def test_a_voided_return_reopens_its_quantity(auth_api, sold, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 4}],
    }, format='json')
    rtn_id = res.json()['id']

    auth_api.delete(reverse('sales-return-detail', args=[rtn_id]))

    # Nothing came back after all, so the full quantity is returnable again.
    assert Decimal(returnable_lines(sold)[0]['quantity_open']) == Decimal('4')


def test_a_voided_invoice_cannot_be_returned_against(auth_api, sold, gl_accounts):
    auth_api.delete(reverse('invoice-detail', args=[sold.pk]))

    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 1}],
    }, format='json')

    assert res.status_code == 400
    assert 'dibatalkan' in str(res.json())


# ── The refund arithmetic ─────────────────────────────────────────────────────

def test_refund_of_a_plain_line_is_price_times_quantity(sold):
    line = sold.items.first()

    parts = compute_refund(sold, [(line, Decimal('2'))])

    assert parts['total_refund'] == Decimal('20000.00')


def test_invoice_level_discount_is_apportioned_not_ignored(auth_api, stock, gl_accounts):
    """4 x 10.000 with a 4.000 invoice discount. Returning half must give back
    half the discount too, or the clinic refunds more than it took."""
    invoice = _sell(auth_api, stock, gl_accounts, quantity=4, price=10000, discount=4000)
    line = invoice.items.first()

    parts = compute_refund(invoice, [(line, Decimal('2'))])

    assert parts['net'] == Decimal('20000.00')
    assert parts['invoice_discount'] == Decimal('2000.00')
    assert parts['total_refund'] == Decimal('18000.00')


def test_tax_and_charges_ride_along_with_the_refund(auth_api, stock, gl_accounts):
    invoice = _sell(auth_api, stock, gl_accounts, quantity=4, price=10000,
                    tax=4000, charges=800)
    line = invoice.items.first()

    parts = compute_refund(invoice, [(line, Decimal('1'))])

    # A quarter of the invoice comes back, so a quarter of each component does.
    assert parts['tax'] == Decimal('1000.00')
    assert parts['additional_charges'] == Decimal('200.00')
    assert parts['total_refund'] == Decimal('11200.00')


def test_refund_never_goes_negative(auth_api, stock, gl_accounts):
    """A discount larger than the line's value is a data problem, not a request
    for the patient to pay the clinic for taking goods back."""
    invoice = _sell(auth_api, stock, gl_accounts, quantity=1, price=10000, discount=10000)
    line = invoice.items.first()

    parts = compute_refund(invoice, [(line, Decimal('1'))])

    assert parts['total_refund'] >= Decimal('0')


# ── Stock ─────────────────────────────────────────────────────────────────────

def test_creating_a_return_moves_no_stock_until_the_journal_runs(auth_api, sold, stock, gl_accounts):
    before = _batches()
    line = sold.items.first()

    auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 2}],
    }, format='json')

    assert _batches() == before


def test_the_journal_run_puts_restocked_units_back(auth_api, sold, stock, gl_accounts):
    line = sold.items.first()
    auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 2}],
    }, format='json')

    stock['batch'].refresh_from_db()
    before = stock['batch'].quantity_remaining
    _run_journal(auth_api)
    stock['batch'].refresh_from_db()

    assert stock['batch'].quantity_remaining == before + Decimal('2')


def test_a_line_marked_unsellable_is_refunded_but_not_restocked(auth_api, sold, stock, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'reason': 'damaged',
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 2, 'restock': False}],
    }, format='json')
    assert res.status_code == 201

    stock['batch'].refresh_from_db()
    before = stock['batch'].quantity_remaining
    _run_journal(auth_api)
    stock['batch'].refresh_from_db()

    assert stock['batch'].quantity_remaining == before
    # The money still goes back — the patient does not keep paying for a broken
    # item just because the clinic cannot resell it.
    assert Decimal(res.json()['total_refund']) == Decimal('20000')


def test_a_service_is_never_restocked_even_if_the_client_asks(auth_api, stock, gl_accounts):
    service = InventoryItemFactory(is_service=True, selling_price=50000)
    res = auth_api.post(reverse('invoice-create'), {
        'warehouse_id': stock['warehouse'].id,
        'payment_method_id': gl_accounts['cash_method'].id,
        'discount': '0', 'tax': '0', 'additional_charges': '0',
        'grand_total': '50000',
        'items': [{'item_id': service.id, 'price': '50000', 'quantity': 1}],
    }, format='json')
    invoice = Invoice.objects.get(pk=res.json()['id'])
    line = invoice.items.first()

    res = auth_api.post(reverse('sales-returns'), {
        'invoice': invoice.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 1, 'restock': True}],
    }, format='json')
    assert res.status_code == 201

    assert SalesReturnItem.objects.get(sales_return_id=res.json()['id']).restock is False


# ── The journal ───────────────────────────────────────────────────────────────

def test_a_full_return_returns_every_balance_and_batch_to_baseline(
        auth_api, stock, gl_accounts):
    """The load-bearing test. Sell, return everything, post both."""
    baseline_balances = _balances()
    baseline_batches = _batches()

    invoice = _sell(auth_api, stock, gl_accounts, quantity=4, price=10000)
    _run_journal(auth_api)

    line = invoice.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': invoice.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 4}],
    }, format='json')
    assert res.status_code == 201, res.content
    _run_journal(auth_api)

    assert _balances() == baseline_balances
    assert _batches() == baseline_batches


def test_a_full_return_of_a_discounted_taxed_invoice_also_nets_to_zero(
        auth_api, stock, gl_accounts):
    baseline_balances = _balances()
    baseline_batches = _batches()

    invoice = _sell(auth_api, stock, gl_accounts, quantity=4, price=10000,
                    discount=5000, tax=3500, charges=1000)
    _run_journal(auth_api)

    line = invoice.items.first()
    auth_api.post(reverse('sales-returns'), {
        'invoice': invoice.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 4}],
    }, format='json')
    _run_journal(auth_api)

    assert _balances() == baseline_balances
    assert _batches() == baseline_batches


def test_the_posted_entry_is_balanced_and_tagged(auth_api, sold, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 2}],
    }, format='json')
    _run_journal(auth_api)

    rtn = SalesReturn.objects.get(pk=res.json()['id'])
    entry = rtn.journal_entries.get()

    assert rtn.posting_status == 'posted'
    assert entry.source_type == 'sales_return'
    assert entry.total_debit == entry.total_credit
    assert entry.lines.filter(source_type='sales_return').count() == entry.lines.count()


def test_the_refund_credits_the_named_cash_account(auth_api, sold, gl_accounts):
    line = sold.items.first()
    auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['bank'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 2}],
    }, format='json')
    _run_journal(auth_api)

    credited = gl_accounts['bank'].ledger_entries.filter(
        source_type='sales_return', entry_type='credit')

    assert credited.count() == 1
    assert credited.first().amount == Decimal('20000.00')


def test_refund_out_of_a_non_cash_account_is_refused(auth_api, sold, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        # Inventory is an asset, but it is not somewhere money lives.
        'refund_account': gl_accounts['inventory_asset'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 1}],
    }, format='json')

    assert res.status_code == 400
    assert 'kas/bank' in str(res.json())


def test_voiding_a_posted_return_undoes_both_the_stock_and_the_ledger(
        auth_api, stock, gl_accounts):
    invoice = _sell(auth_api, stock, gl_accounts, quantity=4, price=10000)
    _run_journal(auth_api)

    after_sale_balances = _balances()
    after_sale_batches = _batches()

    line = invoice.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': invoice.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 4}],
    }, format='json')
    _run_journal(auth_api)

    auth_api.delete(reverse('sales-return-detail', args=[res.json()['id']]))

    assert _balances() == after_sale_balances
    assert _batches() == after_sale_batches


def test_an_unposted_return_voids_without_touching_anything(auth_api, sold, gl_accounts):
    before_balances = _balances()
    before_batches = _batches()

    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 2}],
    }, format='json')

    res2 = auth_api.delete(reverse('sales-return-detail', args=[res.json()['id']]))

    assert res2.status_code == 200
    assert _balances() == before_balances
    assert _batches() == before_batches


def test_a_voided_return_is_never_swept(auth_api, sold, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 2}],
    }, format='json')
    auth_api.delete(reverse('sales-return-detail', args=[res.json()['id']]))

    _run_journal(auth_api)

    rtn = SalesReturn.objects.get(pk=res.json()['id'])
    assert rtn.posting_status == 'unposted'
    assert rtn.journal_entries.count() == 0


def test_a_return_cannot_be_voided_twice(auth_api, sold, gl_accounts):
    line = sold.items.first()
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': line.id, 'quantity': 1}],
    }, format='json')
    rtn_id = res.json()['id']

    assert auth_api.delete(reverse('sales-return-detail', args=[rtn_id])).status_code == 200
    assert auth_api.delete(reverse('sales-return-detail', args=[rtn_id])).status_code == 400


# ── Preview and the API surface ───────────────────────────────────────────────

def test_preview_promises_what_the_post_delivers(auth_api, sold, gl_accounts):
    line = sold.items.first()
    items = [{'invoice_item_id': line.id, 'quantity': 3}]

    preview = auth_api.post(reverse('sales-return-preview'),
                            {'invoice': sold.pk, 'items': items}, format='json')
    created = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk, 'refund_account': gl_accounts['cash'].id, 'items': items,
    }, format='json')

    assert Decimal(preview.json()['total_refund']) == Decimal(created.json()['total_refund'])


def test_the_return_inherits_the_invoices_branch_not_the_request_header(
        auth_api, sold, gl_accounts, db):
    """A refund booked into a different branch than the revenue it reverses
    leaves both branches wrong."""
    from .factories import BranchFactory

    # --nomigrations skips the 0113 backfill, so the invoice has no branch
    # unless one is put there. Give it one: the point of the test is that the
    # header loses to the invoice, which needs the invoice to have an answer.
    home = BranchFactory(code='HOME', is_default=True)
    sold.branch = home
    sold.save(update_fields=['branch'])

    other = BranchFactory(code='ELSEWHERE')
    line = sold.items.first()

    res = auth_api.post(
        reverse('sales-returns'),
        {'invoice': sold.pk, 'refund_account': gl_accounts['cash'].id,
         'items': [{'invoice_item_id': line.id, 'quantity': 1}]},
        format='json', HTTP_X_BRANCH_ID=str(other.pk),
    )

    rtn = SalesReturn.objects.get(pk=res.json()['id'])
    assert rtn.branch_id == sold.branch_id


def test_an_empty_item_list_is_refused(auth_api, sold, gl_accounts):
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [],
    }, format='json')

    assert res.status_code == 400


def test_a_line_from_another_invoice_is_refused(auth_api, stock, gl_accounts, sold):
    other = _sell(auth_api, stock, gl_accounts, quantity=1, price=10000)
    foreign_line = other.items.first()

    res = auth_api.post(reverse('sales-returns'), {
        'invoice': sold.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': foreign_line.id, 'quantity': 1}],
    }, format='json')

    assert res.status_code == 400


def test_returnable_endpoint_serves_the_same_numbers_the_post_validates(auth_api, sold):
    res = auth_api.get(reverse('invoice-returnable', args=[sold.pk]))

    assert res.status_code == 200
    payload = res.json()
    assert payload['invoice_number'] == sold.invoice_number
    assert Decimal(payload['lines'][0]['quantity_open']) == Decimal('4')


def test_validate_lines_raises_rather_than_returning_a_flag(sold):
    line = sold.items.first()

    with pytest.raises(SalesReturnError):
        validate_lines(sold, [{'invoice_item_id': line.id, 'quantity': 0}])
