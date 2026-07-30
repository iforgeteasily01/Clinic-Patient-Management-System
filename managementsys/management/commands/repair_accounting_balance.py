"""Rebuild the general ledger so every document — and the ledger as a whole — balances.

Three classes of damage are repaired:

1. Sales invoices posted by the old code, which debited the payment account for the
   full ``grand_total`` but credited revenue only for lines carrying an
   ``InventoryItem`` FK, and never posted ``tax``, ``additional_charges`` or any
   discount. Their cash/revenue block is deleted and re-posted from the invoice.

2. Purchase invoices carrying a single leg — the pre-accrual flow credited the bank
   without debiting inventory. The missing leg is added.

3. ``ChartOfAccounts.balance`` values that drifted away from the journal, because the
   billing-queue path updated balances without writing ``LedgerEntry`` rows. Every
   balance is recomputed from the ledger.

COGS and inventory rows are left alone throughout: they already balance in pairs and
re-deriving them would mean re-running FIFO against stock that has already moved.

Invoices predating the accounting module (no ledger entries at all) are deliberately
untouched — they are imported sales history with no cost or payment data, and posting
them would fabricate revenue.

A dry run performs every change inside a transaction and rolls it back, so the
verification it prints is the real post-repair state. Pass --apply to commit.
"""
from collections import defaultdict
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Sum

from managementsys.models import (
    ChartOfAccounts, Invoice, LedgerEntry, PurchaseInvoice,
)
from managementsys.views.invoice_page import (
    ACC_INVENTORY, DEBIT_NORMAL_TYPES, _lines_from_instances, _post_legs, _revenue_legs,
)

D = Decimal


def _totals(entries):
    dr = sum((e.amount for e in entries if e.entry_type == 'debit'), D('0'))
    cr = sum((e.amount for e in entries if e.entry_type == 'credit'), D('0'))
    return dr, cr


def _is_cost_row(entry):
    """COGS/inventory rows are preserved — they mirror real FIFO stock movement."""
    return (entry.account.account_type == 'cogs'
            or entry.account.account_number == ACC_INVENTORY)


class Command(BaseCommand):
    help = 'Repair unbalanced ledger entries and recompute account balances.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit the repair. Without it the work is rolled back after verifying.')

    def handle(self, *args, **opts):
        apply_changes = opts['apply']
        self.stdout.write(self.style.MIGRATE_HEADING(
            'Repairing ledger' if apply_changes else 'Ledger repair — DRY RUN (rolled back at the end)'
        ))

        for number in (4100000, 2200000, 7100000):
            if not ChartOfAccounts.objects.filter(account_number=number).exists():
                self.stderr.write(self.style.ERROR(
                    f'Account {number} is missing. Run `manage.py migrate` first.'))
                return

        with transaction.atomic():
            inv_stats = self._repair_invoices()
            pur_stats = self._repair_purchases()
            bal_stats = self._recompute_balances()
            self._report(inv_stats, pur_stats, bal_stats)
            self._verify()
            if not apply_changes:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING(
                    '\nDRY RUN — everything above was rolled back. Re-run with --apply.'))
            else:
                self.stdout.write(self.style.SUCCESS('\nChanges committed.'))

    # ── 1. Sales invoices ────────────────────────────────────────────────────
    def _repair_invoices(self):
        stats = defaultdict(int)
        stats['reposted_amount'] = D('0')

        invoice_ids = list(
            LedgerEntry.objects.filter(invoice__isnull=False)
            .values_list('invoice_id', flat=True).distinct()
        )
        invoices = (
            Invoice.objects.filter(id__in=invoice_ids)
            .select_related('payment_method')
            .prefetch_related(
                'items__item__item_category__revenue_account',
                'ledger_entries__account',
            )
        )

        for invoice in invoices:
            entries = list(invoice.ledger_entries.all())
            cost_rows = [e for e in entries if _is_cost_row(e)]
            stale = [e for e in entries if not _is_cost_row(e)]

            if stale:
                LedgerEntry.objects.filter(id__in=[e.id for e in stale]).delete()
            stats['deleted_rows'] += len(stale)

            if invoice.is_voided:
                # A void must leave no trace in the P&L. The cash/revenue block is
                # gone; the COGS pairs remain and should already net to zero.
                dr, cr = _totals(cost_rows)
                stats['voided'] += 1
                if dr != cr:
                    stats['voided_cost_imbalance'] += 1
                continue

            legs = _revenue_legs(invoice, _lines_from_instances(list(invoice.items.all())))
            _post_legs(invoice, legs)
            stats['reposted'] += 1
            stats['new_rows'] += len(legs)
            stats['reposted_amount'] += sum(
                (a for _ac, side, a, _d in legs if side == 'debit'), D('0'))

        return stats

    # ── 2. Purchase invoices ─────────────────────────────────────────────────
    def _repair_purchases(self):
        stats = defaultdict(int)
        stats['added_amount'] = D('0')
        inventory = ChartOfAccounts.objects.filter(account_number=ACC_INVENTORY).first()

        purchase_ids = list(
            LedgerEntry.objects.filter(purchase_invoice__isnull=False)
            .values_list('purchase_invoice_id', flat=True).distinct()
        )
        for purchase in (PurchaseInvoice.objects.filter(id__in=purchase_ids)
                         .prefetch_related('ledger_entries__account')):
            entries = list(purchase.ledger_entries.all())
            dr, cr = _totals(entries)
            delta = dr - cr
            if delta == 0:
                continue

            if purchase.is_voided:
                # Nothing was ever paid and the document is cancelled; the stray
                # leg is the remnant of an incomplete reversal.
                LedgerEntry.objects.filter(id__in=[e.id for e in entries]).delete()
                stats['voided_cleared'] += 1
                stats['deleted_rows'] += len(entries)
                continue

            if delta < 0:
                # Bank was credited but the goods were never debited to inventory.
                if inventory is None:
                    stats['skipped_no_account'] += 1
                    continue
                LedgerEntry.objects.create(
                    account=inventory,
                    date=purchase.purchase_date,
                    description=f'Pembelian {purchase.internal_id} – penyeimbang persediaan',
                    entry_type='debit',
                    amount=-delta,
                    source_type='purchase',
                    purchase_invoice=purchase,
                )
                stats['inventory_leg_added'] += 1
                stats['added_amount'] += -delta
            else:
                stats['unexpected_debit_surplus'] += 1

        return stats

    # ── 3. Stored balances ───────────────────────────────────────────────────
    def _recompute_balances(self):
        stats = defaultdict(int)
        agg = defaultdict(lambda: {'debit': D('0'), 'credit': D('0')})
        for row in (LedgerEntry.objects.values('account_id', 'entry_type')
                    .annotate(total=Sum('amount'))):
            agg[row['account_id']][row['entry_type']] = row['total'] or D('0')

        for account in ChartOfAccounts.objects.all():
            sums = agg.get(account.id, {'debit': D('0'), 'credit': D('0')})
            dr, cr = sums['debit'], sums['credit']
            derived = (dr - cr) if account.account_type in DEBIT_NORMAL_TYPES else (cr - dr)
            if account.balance != derived:
                stats['corrected'] += 1
                ChartOfAccounts.objects.filter(pk=account.pk).update(balance=derived)
        return stats

    # ── Reporting ────────────────────────────────────────────────────────────
    def _report(self, inv, pur, bal):
        w = self.stdout.write
        w('\nSales invoices')
        w(f'  re-posted                    : {inv["reposted"]:,}')
        w(f'  voided (block removed)       : {inv["voided"]:,}')
        w(f'  stale rows deleted           : {inv["deleted_rows"]:,}')
        w(f'  rows written                 : {inv["new_rows"]:,}')
        w(f'  value re-posted              : {inv["reposted_amount"]:,.2f}')
        if inv['voided_cost_imbalance']:
            w(self.style.WARNING(
                f'  voided invoices whose COGS rows do not net to zero: '
                f'{inv["voided_cost_imbalance"]:,}'))

        w('\nPurchase invoices')
        w(f'  inventory leg added          : {pur["inventory_leg_added"]:,}')
        w(f'  value added                  : {pur["added_amount"]:,.2f}')
        w(f'  voided docs cleared          : {pur["voided_cleared"]:,}')
        if pur['unexpected_debit_surplus']:
            w(self.style.WARNING(
                f'  live purchases with a debit surplus: {pur["unexpected_debit_surplus"]:,}'))

        w('\nAccount balances')
        w(f'  recomputed from ledger       : {bal["corrected"]:,}')

    def _verify(self):
        w = self.stdout.write
        dr = LedgerEntry.objects.filter(entry_type='debit').aggregate(t=Sum('amount'))['t'] or D('0')
        cr = LedgerEntry.objects.filter(entry_type='credit').aggregate(t=Sum('amount'))['t'] or D('0')
        w('\nVerification')
        w(f'  total debits                 : {dr:,.2f}')
        w(f'  total credits                : {cr:,.2f}')
        style = self.style.SUCCESS if dr == cr else self.style.ERROR
        w(style(f'  difference                   : {dr - cr:,.2f}'))

        unbalanced = 0
        for field in ('invoice', 'purchase_invoice', 'transfer'):
            grouped = defaultdict(lambda: [D('0'), D('0')])
            for row in (LedgerEntry.objects.exclude(**{f'{field}__isnull': True})
                        .values(f'{field}_id', 'entry_type').annotate(t=Sum('amount'))):
                idx = 0 if row['entry_type'] == 'debit' else 1
                grouped[row[f'{field}_id']][idx] += row['t'] or D('0')
            bad = sum(1 for d, c in grouped.values() if d != c)
            unbalanced += bad
            w(f'  unbalanced {field:<17}: {bad:,}')

        if dr == cr and unbalanced == 0:
            w(self.style.SUCCESS('  ledger is in balance'))
