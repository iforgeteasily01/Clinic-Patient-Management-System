"""Randomised simulation of the kartu stok against the real endpoints.

Each run drives one clinic through a long random sequence of everything that
moves stock — stock-in, purchases (and their edits/voids), sales to patients
and walk-ins (and their edits/voids), journal runs, returns (restocked or not,
and their voids), stock-outs, pencacahan, production and stock opname — using
the same HTTP endpoints the app uses.

Alongside, it keeps its **own model** of what the card should list: one
expected row per movement, with its status, quantity, warehouse and patient.
After every operation it checks:

* the card lists exactly the expected rows (nothing missing, nothing extra,
  nothing in the wrong state);
* the running balance is the cumulative sum of the applied rows, and never
  dips below zero;
* the balance the card arrives at equals what the FIFO batches actually hold,
  for the item as a whole and for each warehouse;
and, periodically, that paging, a date window and a kind filter return the
same rows and the same per-row balances as the unfiltered card.

Operations are only chosen when they are valid against stock that is *really*
free — on hand minus sales still waiting for the journal run — so a correct
system never hits a FIFO shortfall here. Shortfalls are tested separately at
the bottom, because there the card is *supposed* to disagree with the batches.

``STOCK_SIM_SEEDS`` / ``STOCK_SIM_STEPS`` widen a run from the command line.
"""
import datetime
import os
import random
from collections import Counter
from decimal import Decimal

import pytest
from django.urls import reverse

from managementsys.models import (
    InventoryBatch, JAKARTA_TZ, Patient, PencacahanRecord, StockOutLog, Supplier,
)

from .factories import ChartOfAccountsFactory, InventoryItemFactory, WarehouseFactory

SEEDS = range(int(os.environ.get('STOCK_SIM_SEEDS', '6')))
STEPS = int(os.environ.get('STOCK_SIM_STEPS', '60'))
REASONS = ['expired', 'damaged', 'lost', 'internal_use', 'sample', 'transfer', 'other']


def _today():
    return datetime.datetime.now(JAKARTA_TZ).date()


class Sim:
    def __init__(self, api, gl, rng):
        self.api, self.gl, self.rng = api, gl, rng
        self.whs = [WarehouseFactory(name='Gudang Utama'), WarehouseFactory(name='Gudang Klinik')]
        self.items = [InventoryItemFactory(name=n, unit_small='pcs')
                      for n in ('Serum A', 'Krim B', 'Toner C')]
        self.patients = [Patient.objects.create(patient_no=f'P00000{i}', name=n)
                         for i, n in enumerate(('Ani', 'Budi', 'Citra'), 1)]
        self.supplier = Supplier.objects.create(name='PT Sim')
        # expected[item_id] = {key: row}; key identifies the movement.
        self.expected = {it.id: {} for it in self.items}
        self.invoices = {}   # inv_id -> {'lines': [(line_id, item_id, qty)], 'wh', 'patient', 'posted', 'voided', 'returned': Counter}
        self.returns = {}    # rtn_id -> {'inv', 'lines': [(line_id, item_id, qty, restock)], 'posted', 'voided'}
        self.purchases = {}  # pi_id -> {'item', 'wh', 'qty', 'cost', 'voided'}
        self.global_draws = set()   # items with a production draw that has no warehouse
        self.log = Counter()

    # ── stock arithmetic ─────────────────────────────────────────────────────

    def on_hand(self, item_id, wh_id=None):
        qs = InventoryBatch.objects.filter(item_id=item_id)
        if wh_id is not None:
            qs = qs.filter(warehouse_id=wh_id)
        return sum((b.quantity_remaining for b in qs), Decimal('0'))

    def pending_out(self, item_id, wh_id):
        return sum((-r['qty'] for r in self.expected[item_id].values()
                    if r['status'] == 'pending' and r['qty'] < 0 and r['wh'] == wh_id),
                   Decimal('0'))

    def free(self, item_id, wh_id):
        """Stock an operation may take without starving a sale awaiting the journal."""
        return int(self.on_hand(item_id, wh_id) - self.pending_out(item_id, wh_id))

    def _ok(self, res, name):
        if 200 <= res.status_code < 300:
            self.log[name] += 1
            return True
        self.log[f'{name} (refused {res.status_code})'] += 1
        return False

    # ── operations ───────────────────────────────────────────────────────────

    def stock_in(self):
        it, wh = self.rng.choice(self.items), self.rng.choice(self.whs)
        qty = self.rng.randint(1, 40)
        date = _today() - datetime.timedelta(days=self.rng.randint(0, 5))
        res = self.api.post('/api/inventory/stock-in/', {
            'item_id': it.id, 'warehouse_id': wh.id, 'quantity': qty,
            'input_date': date.isoformat()}, format='json')
        if self._ok(res, 'stock_in'):
            self.expected[it.id][('batch', res.json()['id'])] = dict(
                kind='stock_in', doc=('batch', res.json()['id']), qty=Decimal(qty),
                status='applied', wh=wh.id, patient=None)

    def purchase(self):
        it, wh = self.rng.choice(self.items), self.rng.choice(self.whs)
        qty, cost = self.rng.randint(5, 50), self.rng.choice([1000, 2500, 4000])
        res = self.api.post(reverse('accounting-purchases'), self._po(it, wh, qty, cost), format='json')
        if self._ok(res, 'purchase'):
            pid = res.json()['id']
            self.purchases[pid] = dict(item=it, wh=wh, qty=qty, cost=cost, voided=False)
            self.expected[it.id][('purchase_invoice', pid)] = dict(
                kind='purchase', doc=('purchase_invoice', pid), qty=Decimal(qty),
                status='applied', wh=wh.id, patient=None)

    def _po(self, it, wh, qty, cost):
        return {
            'supplier': self.supplier.id, 'warehouse': wh.id,
            'purchase_date': _today().isoformat(),
            'items': [{'line_type': 'stock', 'item': it.id, 'item_name': it.name,
                       'quantity': qty, 'unit': 'pcs', 'unit_cost': cost, 'warehouse': wh.id}],
        }

    def purchase_edit(self):
        live = [p for p, d in self.purchases.items() if not d['voided']]
        if not live:
            return
        pid = self.rng.choice(live)
        d = self.purchases[pid]
        batches = InventoryBatch.objects.filter(purchase_invoice_id=pid)
        sold = int(sum((b.quantity_initial - b.quantity_remaining for b in batches), Decimal('0')))
        # Shrinking takes stock off the shelf, so it is bounded like any other draw.
        low = max(sold, d['qty'] - self.free(d['item'].id, d['wh'].id), 1)
        new_qty = self.rng.randint(low, d['qty'] + 20)
        res = self.api.put(reverse('accounting-purchase-detail', args=[pid]),
                           self._po(d['item'], d['wh'], new_qty, d['cost']), format='json')
        if self._ok(res, 'purchase_edit'):
            d['qty'] = new_qty
            self.expected[d['item'].id][('purchase_invoice', pid)]['qty'] = Decimal(new_qty)

    def purchase_void(self):
        live = [p for p, d in self.purchases.items() if not d['voided']]
        if not live:
            return
        pid = self.rng.choice(live)
        d = self.purchases[pid]
        if self.free(d['item'].id, d['wh'].id) < d['qty']:
            return
        res = self.api.delete(reverse('accounting-purchase-detail', args=[pid]))
        if self._ok(res, 'purchase_void'):
            d['voided'] = True
            del self.expected[d['item'].id][('purchase_invoice', pid)]

    def sale(self):
        wh = self.rng.choice(self.whs)
        picks = self.rng.sample(self.items, self.rng.randint(1, 2))
        lines = []
        for it in picks:
            free = self.free(it.id, wh.id)
            if free >= 1:
                lines.append((it, self.rng.randint(1, min(free, 6))))
        if not lines:
            return
        patient = self.rng.choice(self.patients + [None])
        body = {
            'warehouse_id': wh.id, 'payment_method_id': self.gl['cash_method'].id,
            'grand_total': str(sum(10000 * q for _, q in lines)),
            'items': [{'item_id': it.id, 'price': '10000', 'quantity': q} for it, q in lines],
        }
        if patient:
            body['patient_no'] = patient.patient_no
        res = self.api.post(reverse('invoice-create'), body, format='json')
        if not self._ok(res, 'sale'):
            return
        inv_id = res.json()['id']
        from managementsys.models import InvoiceItem
        db_lines = list(InvoiceItem.objects.filter(invoice_id=inv_id).order_by('id'))
        self.invoices[inv_id] = dict(
            lines=[(ln.id, ln.item_id, Decimal(ln.quantity)) for ln in db_lines],
            wh=wh.id, patient=patient.patient_no if patient else None,
            posted=False, voided=False, returned=Counter())
        for ln in db_lines:
            self.expected[ln.item_id][('invoice', inv_id, ln.id)] = dict(
                kind='sale', doc=('invoice', inv_id), qty=-Decimal(ln.quantity),
                status='pending', wh=wh.id, patient=self.invoices[inv_id]['patient'])

    def journal(self):
        res = self.api.post(reverse('accounting-journal-run'),
                            {'date_to': datetime.date.today().isoformat()}, format='json')
        if self._ok(res, 'journal_run'):
            for rows in self.expected.values():
                for r in rows.values():
                    if r['status'] == 'pending':
                        r['status'] = 'applied'
            for d in self.invoices.values():
                d['posted'] = True
            for d in self.returns.values():
                d['posted'] = True

    def invoice_edit(self):
        cands = [i for i, d in self.invoices.items()
                 if not d['voided'] and len(d['lines']) == 1 and not d['returned']]
        if not cands:
            return
        inv_id = self.rng.choice(cands)
        d = self.invoices[inv_id]
        _, item_id, old = d['lines'][0]
        # A posted edit re-draws at once: an increase is bounded by free stock.
        room = self.free(item_id, d['wh']) if d['posted'] else self.free(item_id, d['wh'])
        new = self.rng.randint(1, int(old) + max(0, min(room, 4)))
        res = self.api.patch(reverse('invoice-detail', args=[inv_id]), {
            'grand_total': 10000 * new,
            'items': [{'item_id': item_id, 'quantity': new, 'price': 10000}]}, format='json')
        if not self._ok(res, 'invoice_edit'):
            return
        from managementsys.models import InvoiceItem
        ln = InvoiceItem.objects.get(invoice_id=inv_id)       # lines are replaced wholesale
        del self.expected[item_id][('invoice', inv_id, d['lines'][0][0])]
        d['lines'] = [(ln.id, item_id, Decimal(new))]
        self.expected[item_id][('invoice', inv_id, ln.id)] = dict(
            kind='sale', doc=('invoice', inv_id), qty=-Decimal(new),
            status='applied' if d['posted'] else 'pending', wh=d['wh'], patient=d['patient'])

    def invoice_void(self):
        cands = [i for i, d in self.invoices.items() if not d['voided'] and not d['returned']]
        if not cands:
            return
        inv_id = self.rng.choice(cands)
        d = self.invoices[inv_id]
        res = self.api.delete(reverse('invoice-detail', args=[inv_id]))
        if self._ok(res, 'invoice_void'):
            d['voided'] = True
            for line_id, item_id, _ in d['lines']:
                del self.expected[item_id][('invoice', inv_id, line_id)]

    def sales_return(self):
        cands = [i for i, d in self.invoices.items() if not d['voided'] and d['posted']
                 and any(q - d['returned'][lid] > 0 for lid, _, q in d['lines'])]
        if not cands:
            return
        inv_id = self.rng.choice(cands)
        d = self.invoices[inv_id]
        lid, item_id, q = self.rng.choice([ln for ln in d['lines'] if ln[2] - d['returned'][ln[0]] > 0])
        qty = self.rng.randint(1, int(q - d['returned'][lid]))
        restock = self.rng.random() < 0.75
        res = self.api.post(reverse('sales-returns'), {
            'invoice': inv_id, 'refund_account': self.gl['cash'].id,
            'reason': 'customer_change' if restock else 'damaged',
            'items': [{'invoice_item_id': lid, 'quantity': qty, 'restock': restock}],
        }, format='json')
        if not self._ok(res, 'return' if restock else 'return_unsellable'):
            return
        rid = res.json()['id']
        d['returned'][lid] += qty
        self.returns[rid] = dict(inv=inv_id, lines=[(lid, item_id, qty, restock)],
                                 posted=False, voided=False)
        if restock:
            self.expected[item_id][('sales_return', rid)] = dict(
                kind='sale_return', doc=('sales_return', rid), qty=Decimal(qty),
                status='pending', wh=d['wh'], patient=d['patient'])

    def return_void(self):
        cands = [r for r, d in self.returns.items() if not d['voided']]
        if not cands:
            return
        rid = self.rng.choice(cands)
        d = self.returns[rid]
        lid, item_id, qty, restock = d['lines'][0]
        inv = self.invoices[d['inv']]
        # Voiding a posted, restocked return takes the units back off the shelf.
        if d['posted'] and restock and self.free(item_id, inv['wh']) < qty:
            return
        res = self.api.delete(reverse('sales-return-detail', args=[rid]))
        if self._ok(res, 'return_void'):
            d['voided'] = True
            inv['returned'][lid] -= qty
            if restock:
                del self.expected[item_id][('sales_return', rid)]

    def stock_out(self):
        it, wh = self.rng.choice(self.items), self.rng.choice(self.whs)
        free = self.free(it.id, wh.id)
        if free < 1:
            return
        qty = self.rng.randint(1, min(free, 8))
        res = self.api.post('/api/inventory/stock-out/', {
            'item_id': it.id, 'warehouse_id': wh.id, 'quantity': qty,
            'reason': self.rng.choice(REASONS), 'notes': 'sim'}, format='json')
        if self._ok(res, 'stock_out'):
            log = StockOutLog.objects.latest('id')
            self.expected[it.id][('stock_out', log.id)] = dict(
                kind='stock_out', doc=('stock_out', log.id), qty=-Decimal(qty),
                status='applied', wh=wh.id, patient=None)

    def pencacahan(self):
        src, tgt = self.rng.sample(self.items, 2)
        swh, twh = self.rng.choice(self.whs), self.rng.choice(self.whs)
        free = self.free(src.id, swh.id)
        if free < 1:
            return
        sq, tq = self.rng.randint(1, min(free, 3)), self.rng.randint(5, 30)
        res = self.api.post('/api/inventory/pencacahan/', {
            'source_item_id': src.id, 'source_warehouse_id': swh.id, 'source_quantity': sq,
            'target_item_id': tgt.id, 'target_warehouse_id': twh.id, 'target_quantity': tq,
        }, format='json')
        if self._ok(res, 'pencacahan'):
            rec = PencacahanRecord.objects.latest('id')
            self.expected[src.id][('pencacahan_out', rec.id)] = dict(
                kind='pencacahan_out', doc=('pencacahan', rec.id), qty=-Decimal(sq),
                status='applied', wh=swh.id, patient=None)
            self.expected[tgt.id][('pencacahan_in', rec.id)] = dict(
                kind='pencacahan_in', doc=('pencacahan', rec.id), qty=Decimal(tq),
                status='applied', wh=twh.id, patient=None)

    def production(self):
        out_item = self.rng.choice(self.items)
        ingredients = [it for it in self.items if it.id != out_item.id]
        out_wh = self.rng.choice(self.whs)
        lines, plan = [], []
        for it in self.rng.sample(ingredients, self.rng.randint(1, 2)):
            if self.rng.random() < 0.2:
                # "Any warehouse": drawn FIFO across both, so bounded by the
                # tighter of the two free figures to stay shortfall-free.
                free = min(self.free(it.id, w.id) for w in self.whs)
                src = None
            else:
                src = self.rng.choice(self.whs)
                free = self.free(it.id, src.id)
            if free < 1:
                continue
            q = self.rng.randint(1, min(free, 3))
            line = {'item_id': it.id, 'quantity': q, 'unit': 'small'}
            if src:
                line['source_warehouse_id'] = src.id
            lines.append(line)
            plan.append((it, q, src))
        if not lines:
            return
        oq = self.rng.randint(1, 10)
        res = self.api.post('/api/inventory/production/runs/', {
            'output_item': out_item.id, 'output_warehouse': out_wh.id,
            'output_quantity': oq, 'lines': lines}, format='json')
        if not self._ok(res, 'production'):
            return
        run_id = res.json()['id']
        self.expected[out_item.id][('production_in', run_id)] = dict(
            kind='production_in', doc=('production_run', run_id), qty=Decimal(oq),
            status='applied', wh=out_wh.id, patient=None)
        for it, q, src in plan:
            if src is None:
                self.global_draws.add(it.id)
            self.expected[it.id][('production_out', run_id, it.id)] = dict(
                kind='production_out', doc=('production_run', run_id), qty=-Decimal(q),
                status='applied', wh=src.id if src else None, patient=None)

    def opname(self):
        it, wh = self.rng.choice(self.items), self.rng.choice(self.whs)
        free = self.free(it.id, wh.id)
        if free < 1:
            return
        system = int(self.on_hand(it.id, wh.id))
        shortage = self.rng.randint(1, min(free, 4))
        row = {'item_id': it.id, 'shelf1_qty': system - shortage, 'shelf2_qty': 0,
               'system_qty': system}
        res = self.api.post('/api/stock-opname/', {
            'date': _today().isoformat(), 'conducted_by': 'Sim', 'warehouse_id': wh.id,
            'items': [row]}, format='json')
        if not self._ok(res, 'opname_draft'):
            return
        sid = res.json()['id']
        res = self.api.post(reverse('stock-opname-complete', args=[sid]),
                            {'items': [dict(row, is_loss=True)]}, format='json')
        if self._ok(res, 'opname'):
            self.expected[it.id][('opname', sid)] = dict(
                kind='opname', doc=('stock_opname', sid), qty=-Decimal(shortage),
                status='applied', wh=wh.id, patient=None)

    OPS = [(stock_in, 6), (purchase, 5), (purchase_edit, 2), (purchase_void, 1),
           (sale, 14), (journal, 5), (invoice_edit, 3), (invoice_void, 2),
           (sales_return, 4), (return_void, 2), (stock_out, 4), (pencacahan, 2),
           (production, 2), (opname, 2)]

    def step(self):
        ops, weights = zip(*self.OPS)
        self.rng.choices(ops, weights)[0](self)

    # ── checks ───────────────────────────────────────────────────────────────

    def card(self, item, **params):
        res = self.api.get(reverse('inventory-item-movements', args=[item.pk]),
                           {'page_size': 500, **params})
        assert res.status_code == 200, res.content
        return res.json()

    @staticmethod
    def _sig(row):
        qty = Decimal(row['quantity_in'] or 0) - Decimal(row['quantity_out'] or 0)
        return (row['kind'], row['document']['type'], row['document']['id'], qty,
                row['status'], row['warehouse']['id'] if row['warehouse'] else None,
                row['patient']['patient_no'] if row['patient'] else None)

    @staticmethod
    def _exp_sig(r):
        return (r['kind'], r['doc'][0], r['doc'][1], r['qty'], r['status'], r['wh'], r['patient'])

    def check(self, where, deep=False):
        for it in self.items:
            card = self.card(it)
            got = Counter(self._sig(r) for r in card['results'])
            want = Counter(self._exp_sig(r) for r in self.expected[it.id].values())
            assert got == want, (
                f'{where}: rows for {it.name} differ\n'
                f'  unexpected: {sorted((got - want).elements())}\n'
                f'  missing:    {sorted((want - got).elements())}')

            # Running balance: cumulative applied quantity, oldest first, never < 0.
            bal = Decimal('0')
            for r in reversed(card['results']):
                if r['status'] == 'applied':
                    bal += Decimal(r['quantity_in'] or 0) - Decimal(r['quantity_out'] or 0)
                    assert Decimal(r['balance_after']) == bal, f'{where}: broken balance chain'
                    assert bal >= 0, f'{where}: {it.name} balance went negative at {r}'
                else:
                    assert r['balance_after'] is None

            assert Decimal(card['history_balance']) == bal
            assert Decimal(card['on_hand']) == self.on_hand(it.id), (
                f'{where}: on_hand field wrong for {it.name}')
            assert Decimal(card['discrepancy']) == 0, (
                f'{where}: {it.name} history {card["history_balance"]} vs batches {card["on_hand"]}')

            for wh in self.whs:
                wcard = self.card(it, warehouse_id=wh.id)
                assert all(r['warehouse'] and r['warehouse']['id'] == wh.id for r in wcard['results'])
                if it.id not in self.global_draws:
                    assert Decimal(wcard['discrepancy']) == 0, (
                        f'{where}: {it.name} @ {wh.name} history {wcard["history_balance"]} '
                        f'vs batches {wcard["on_hand"]}')

            if deep:
                self._check_views(it, card)

    def _check_views(self, it, full):
        rows = full['results']
        # Paging returns the same rows in the same order.
        paged, page = [], 1
        while True:
            res = self.api.get(reverse('inventory-item-movements', args=[it.pk]),
                               {'page_size': 7, 'page': page}).json()
            paged += res['results']
            if page >= res['num_pages']:
                break
            page += 1
        assert [self._sig(r) for r in paged] == [self._sig(r) for r in rows]

        # A date window: same rows, same per-row balance, and an opening balance
        # that is everything applied before it.
        dates = sorted({r['date'] for r in rows})
        if dates:
            lo = self.rng.choice(dates)
            hi = self.rng.choice([d for d in dates if d >= lo])
            win = self.card(it, date_from=lo, date_to=hi)
            exp = [r for r in rows if lo <= r['date'] <= hi]
            assert [(self._sig(r), r['balance_after']) for r in win['results']] == \
                   [(self._sig(r), r['balance_after']) for r in exp]
            opening = sum((Decimal(r['quantity_in'] or 0) - Decimal(r['quantity_out'] or 0)
                           for r in rows if r['status'] == 'applied' and r['date'] < lo),
                          Decimal('0'))
            assert Decimal(win['opening_balance']) == opening
            closing = opening + Decimal(win['totals']['in']) - Decimal(win['totals']['out'])
            applied = [r for r in exp if r['status'] == 'applied']
            if applied:
                assert Decimal(applied[0]['balance_after']) == closing

        # A kind filter only narrows; it never changes a row.
        kinds = sorted({r['kind'] for r in rows})
        if kinds:
            k = self.rng.choice(kinds)
            assert [self._sig(r) for r in self.card(it, kind=k)['results']] == \
                   [self._sig(r) for r in rows if r['kind'] == k]


@pytest.fixture
def sim_accounts(gl_accounts):
    # The stock-out reasons' expense accounts, so the journal run can post them.
    for number, kind in ((5000900, 'cogs'), (6100005, 'expense'),
                         (6100030, 'expense'), (6300010, 'expense')):
        ChartOfAccountsFactory(account_number=number, name=f'Sim {number}', account_type=kind)
    return gl_accounts


@pytest.fixture
def manager_api(auth_api, auth_user):
    auth_user.role = 'manager'
    auth_user.save(update_fields=['role'])
    return auth_api


@pytest.mark.parametrize('seed', SEEDS)
def test_random_clinic_history_matches_the_card(manager_api, sim_accounts, seed):
    sim = Sim(manager_api, sim_accounts, random.Random(seed))
    for _ in range(4):           # something on the shelves before anyone sells
        sim.stock_in()
    sim.check('after opening stock')
    for n in range(STEPS):
        sim.step()
        sim.check(f'seed {seed} step {n}', deep=(n % 10 == 9))
    sim.journal()
    sim.check(f'seed {seed} final', deep=True)
    print(f'\nseed {seed}:', dict(sorted(sim.log.items())))


# ── Shortfalls: where the card is meant to disagree with the batches ─────────

def test_a_sale_larger_than_the_shelf_shows_up_as_the_gap(manager_api, stock, gl_accounts):
    """Two pending sales together exceed the 100 on hand. FIFO deducts what is
    there and drops the rest, so the batches hit zero while the history says
    -20 — and the card must report exactly that 20, not paper over it."""
    for qty in (60, 60):
        res = manager_api.post(reverse('invoice-create'), {
            'warehouse_id': stock['warehouse'].id,
            'payment_method_id': gl_accounts['cash_method'].id,
            'grand_total': str(10000 * qty),
            'items': [{'item_id': stock['item'].id, 'price': '10000', 'quantity': qty}],
        }, format='json')
        assert res.status_code == 201, res.content
    manager_api.post(reverse('accounting-journal-run'),
                     {'date_to': datetime.date.today().isoformat()}, format='json')

    card = manager_api.get(reverse('inventory-item-movements', args=[stock['item'].pk])).json()
    assert Decimal(card['on_hand']) == 0
    assert Decimal(card['history_balance']) == Decimal('-20')
    assert Decimal(card['discrepancy']) == Decimal('20')


def test_a_journal_run_commits_a_valued_stock_out(manager_api, sim_accounts, stock):
    """Regression, found by the simulation: check_freshness prefetched 'items' on
    every document kind but transfers, and StockOutLog has none — so the first
    write-off of *purchased* (valued) stock crashed every journal run after it."""
    res = manager_api.post('/api/inventory/stock-out/', {
        'item_id': stock['item'].id, 'warehouse_id': stock['warehouse'].id,
        'quantity': 3, 'reason': 'expired'}, format='json')
    assert res.status_code == 200, res.content
    assert res.json()['posting_status'] == 'unposted'      # the fixture batch has value

    res = manager_api.post(reverse('accounting-journal-run'),
                           {'date_to': datetime.date.today().isoformat()}, format='json')
    assert res.status_code == 200, res.content
    assert StockOutLog.objects.get().posting_status == 'posted'
