"""A document voided while a journal commit is running must not be journalled.

The bug: ``commit_preview`` checks every staged document's fingerprint once,
before day one, then posts one transaction per day. ``_commit_entry`` only
refused documents already *posted* — not voided ones — and the void endpoints
read ``posting_status`` without a lock. So a cashier voiding an invoice while
the run was streaming either

  * voided it before its day was reached: the void saw 'unposted' and wrote no
    reversal, then the commit posted it anyway; or
  * voided it while its day was being written: same read, same result.

Either way the books carried a sale that had been cancelled, with nothing
reversing it. The fix is two halves of one lock:

  * ``_commit_entry`` re-checks ``is_voided`` under its FOR UPDATE and skips;
  * the void endpoints take the same row FOR UPDATE before reading
    ``posting_status``, so a void racing a day in flight waits for it and then
    writes the reversal memo.
"""
import datetime
import threading
import time

import pytest
from django.db import connection
from django.urls import reverse

from managementsys.models import Invoice, JournalEntry, LedgerEntry, SalesReturn
from managementsys.services import journal_preview

from .test_journal_preview import _create_invoice, _preview


def _summary(*args, **kwargs):
    from managementsys.views.accounting_page import _build_journal_summary
    return _build_journal_summary(*args, **kwargs)


def _start_commit():
    """A commit generator advanced past its freshness check (the 'start' event)."""
    gen = journal_preview.commit_preview(None, journal_preview.open_draft(), _summary)
    first = next(gen)
    assert first['type'] == 'start', first
    return gen


@pytest.mark.django_db
def test_invoice_voided_before_its_day_is_skipped_not_journalled(auth_api, stock, gl_accounts):
    day_one = _create_invoice(auth_api, stock, gl_accounts,
                              dt=datetime.datetime(2026, 7, 1, 10, 0), quantity=1,
                              grand_total=10000)
    day_two = _create_invoice(auth_api, stock, gl_accounts,
                              dt=datetime.datetime(2026, 7, 2, 10, 0), quantity=1,
                              grand_total=10000)
    _preview(None, datetime.date(2026, 7, 2))

    gen = _start_commit()
    first_day = next(gen)
    assert first_day['type'] == 'day' and first_day['documents'] == 1

    # Mid-run: day one is committed, day two has not been reached.
    res = auth_api.delete(reverse('invoice-detail', args=[day_two.pk]))
    assert res.status_code == 200, res.content

    events = list(gen)
    done = events[-1]
    assert done['type'] == 'done'
    assert done['documents_posted'] == 1
    assert [s['source_label'] for s in done['skipped']] == [day_two.invoice_number]
    assert done['skipped'][0]['reason'] == journal_preview.VoidedDocumentError.reason

    day_one.refresh_from_db()
    day_two.refresh_from_db()
    assert day_one.posting_status == 'posted'
    assert day_two.is_voided
    assert day_two.posting_status == 'unposted'
    assert not JournalEntry.objects.filter(invoice=day_two).exists()
    assert not LedgerEntry.objects.filter(invoice=day_two).exists()


@pytest.mark.django_db
def test_sales_return_voided_mid_commit_is_skipped(auth_api, stock, gl_accounts):
    from .test_sales_returns import _run_journal, _sell

    invoice = _sell(auth_api, stock, gl_accounts)
    _run_journal(auth_api)
    res = auth_api.post(reverse('sales-returns'), {
        'invoice': invoice.pk,
        'refund_account': gl_accounts['cash'].id,
        'items': [{'invoice_item_id': invoice.items.first().id, 'quantity': 1}],
    }, format='json')
    assert res.status_code == 201, res.content
    rtn = SalesReturn.objects.get(pk=res.json()['id'])

    _preview(None, datetime.date.today())
    gen = _start_commit()
    assert auth_api.delete(reverse('sales-return-detail', args=[rtn.pk])).status_code == 200

    done = list(gen)[-1]
    assert done['type'] == 'done'
    assert [s['source_label'] for s in done['skipped']] == [journal_preview.document_label('sales_return', rtn)]

    rtn.refresh_from_db()
    assert rtn.is_voided and rtn.posting_status == 'unposted'
    assert not JournalEntry.objects.filter(sales_return=rtn).exists()


@pytest.mark.django_db(transaction=True)
def test_void_racing_a_day_in_flight_waits_and_writes_the_reversal(
    auth_api, stock, gl_accounts, monkeypatch
):
    """The void must block on the commit's row lock, then see 'posted'.

    Real transactions on two connections: the commit is paused inside its day
    transaction (after locking the invoice, before writing), and a void is
    fired from a second thread meanwhile.
    """
    invoice = _create_invoice(auth_api, stock, gl_accounts,
                              dt=datetime.datetime(2026, 7, 1, 10, 0), quantity=1,
                              grand_total=10000)
    _preview(None, datetime.date(2026, 7, 1))

    in_day = threading.Event()
    release = threading.Event()
    real_write_legs = journal_preview.write_legs

    def paused_write_legs(*args, **kwargs):
        in_day.set()
        assert release.wait(timeout=20), 'test never released the commit'
        return real_write_legs(*args, **kwargs)

    monkeypatch.setattr(journal_preview, 'write_legs', paused_write_legs)

    results = {}

    def run_commit():
        try:
            results['commit'] = list(journal_preview.commit_preview(
                None, journal_preview.open_draft(), _summary))
        finally:
            connection.close()

    def run_void():
        try:
            results['void'] = auth_api.delete(reverse('invoice-detail', args=[invoice.pk]))
        finally:
            connection.close()

    commit_thread = threading.Thread(target=run_commit)
    commit_thread.start()
    assert in_day.wait(timeout=20), 'commit never reached the invoice'

    void_thread = threading.Thread(target=run_void)
    void_thread.start()
    time.sleep(0.5)
    # Still waiting on the commit's FOR UPDATE — it has not read 'unposted'.
    assert void_thread.is_alive(), 'void did not wait for the commit lock'

    release.set()
    commit_thread.join(timeout=30)
    void_thread.join(timeout=30)
    assert not commit_thread.is_alive() and not void_thread.is_alive()

    assert results['commit'][-1]['type'] == 'done'
    assert results['void'].status_code == 200, results['void'].content

    invoice.refresh_from_db()
    assert invoice.is_voided
    assert invoice.posting_status == 'posted'
    # The sale was journalled, so the void had to reverse it — and did.
    assert JournalEntry.objects.filter(invoice=invoice, source_type='invoice').exists()
    assert LedgerEntry.objects.filter(invoice=invoice, source_type='void_memo').exists()
    # Net effect on every account is nil.
    net = {}
    for le in LedgerEntry.objects.filter(invoice=invoice):
        sign = 1 if le.entry_type == 'debit' else -1
        net[le.account_id] = net.get(le.account_id, 0) + sign * le.amount
    assert all(v == 0 for v in net.values()), net
