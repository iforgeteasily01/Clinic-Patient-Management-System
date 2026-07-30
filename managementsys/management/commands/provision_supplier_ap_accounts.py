"""
provision_supplier_ap_accounts
==============================
One-shot / idempotent setup for the per-vendor Accounts Payable structure on a
fresh or production database. Equivalent to migration 0073 but runnable on
demand with a printed roster.

It does three things:
  1. Ensures the AP control account (2100000) under the Liabilities head and the
     Opening Balance Equity offset account (3900000) under the Equity head exist.
  2. Ensures every Supplier has its own AP sub-account (2100001..) linked.
  3. With --backfill, posts the OUTSTANDING balance of each unpaid/partial
     (non-voided) purchase invoice as Cr AP-vendor / Dr Opening Balance Equity.
     Idempotent: an invoice that already carries a saldo-awal AP entry is
     skipped, so re-running never double-posts.

Usage:
    python manage.py provision_supplier_ap_accounts
    python manage.py provision_supplier_ap_accounts --backfill
    python manage.py provision_supplier_ap_accounts --backfill --dry-run
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F

from managementsys.models import (
    ChartOfAccounts, LedgerEntry, PurchaseInvoice, Supplier,
)

OBE_NUMBER         = 3900000
EQUITY_HEAD_NUMBER = 3000000
ZERO = Decimal('0')
SALDO_AWAL_PREFIX = 'Saldo awal utang '


def _ensure_obe_account():
    obe = ChartOfAccounts.objects.filter(account_number=OBE_NUMBER).first()
    if obe:
        return obe
    head = ChartOfAccounts.objects.filter(account_number=EQUITY_HEAD_NUMBER).first()
    if not head:
        head = ChartOfAccounts.objects.create(
            account_number=EQUITY_HEAD_NUMBER, name='Equity',
            account_type='equity', is_head=True, is_system=False,
        )
    return ChartOfAccounts.objects.create(
        account_number=OBE_NUMBER,
        name='Ekuitas Saldo Awal (Opening Balance Equity)',
        account_type='equity', is_system=True, is_head=False, parent=head,
    )


class Command(BaseCommand):
    help = ('Provision per-vendor Accounts Payable accounts and, optionally, '
            'backfill outstanding purchase-invoice balances into them.')

    def add_arguments(self, parser):
        parser.add_argument('--backfill', action='store_true',
                            help='Also post outstanding invoice balances to AP.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Show what would change without writing.')

    def handle(self, *args, **options):
        dry_run  = options['dry_run']
        backfill = options['backfill']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.\n'))

        # ── 1 + 2. Control/OBE accounts and per-vendor AP accounts ────────────
        newly_linked = []
        with transaction.atomic():
            control = Supplier._ensure_ap_control_account()
            obe = _ensure_obe_account()
            for supplier in Supplier.objects.filter(ap_account__isnull=True).order_by('id'):
                newly_linked.append(supplier.name)
                if not dry_run:
                    supplier.ensure_ap_account()
            if dry_run:
                transaction.set_rollback(True)

        self.stdout.write(self.style.SUCCESS(
            f'AP control account: {control.account_number} · {control.name}'))
        self.stdout.write(self.style.SUCCESS(
            f'Opening Balance Equity: {obe.account_number} · {obe.name}'))
        verb = 'Would link' if dry_run else 'Linked'
        if newly_linked:
            self.stdout.write(self.style.SUCCESS(
                f'\n{verb} AP accounts for {len(newly_linked)} supplier(s):'))
            for name in sorted(newly_linked):
                self.stdout.write(f'  + {name}')
        else:
            self.stdout.write('\nAll suppliers already have an AP account.')

        # ── 3. Optional outstanding backfill ─────────────────────────────────
        if backfill:
            self._backfill(obe, dry_run)

        # ── Roster ────────────────────────────────────────────────────────────
        self.stdout.write(self.style.SUCCESS('\nVendor AP accounts:'))
        for s in Supplier.objects.select_related('ap_account').order_by('name'):
            if s.ap_account_id:
                self.stdout.write(
                    f'  {s.name}: {s.ap_account.account_number} '
                    f'(saldo {s.ap_account.balance})')
            else:
                self.stdout.write(f'  {s.name}: — (no account)')

    def _backfill(self, obe, dry_run):
        outstanding = (
            PurchaseInvoice.objects
            .filter(is_voided=False, status__in=['unpaid', 'partial'])
            .select_related('supplier', 'supplier__ap_account')
            .order_by('id')
        )
        posted = []
        skipped = 0
        obe_delta = ZERO
        entries = []

        for inv in outstanding:
            balance_due = (inv.total_amount or ZERO) - (inv.amount_paid or ZERO)
            if balance_due <= 0:
                continue
            # Idempotency: skip if a saldo-awal AP entry already exists.
            already = LedgerEntry.objects.filter(
                purchase_invoice=inv,
                description__startswith=SALDO_AWAL_PREFIX,
            ).exists()
            if already:
                skipped += 1
                continue
            supplier = inv.supplier
            ap_acct = supplier.ensure_ap_account() if not dry_run else supplier.ap_account
            if ap_acct is None:
                continue

            posted.append((inv.internal_id, supplier.name, balance_due))
            obe_delta += balance_due
            if dry_run:
                continue

            ChartOfAccounts.objects.filter(pk=ap_acct.pk).update(
                balance=F('balance') + balance_due)
            entries.append(LedgerEntry(
                account=ap_acct, date=inv.purchase_date,
                description=f'{SALDO_AWAL_PREFIX}{inv.internal_id} — {supplier.name}',
                entry_type='credit', amount=balance_due,
                source_type='purchase', purchase_invoice=inv,
            ))
            entries.append(LedgerEntry(
                account=obe, date=inv.purchase_date,
                description=f'{SALDO_AWAL_PREFIX}{inv.internal_id} — {supplier.name}',
                entry_type='debit', amount=balance_due,
                source_type='purchase', purchase_invoice=inv,
            ))

        if not dry_run and entries:
            LedgerEntry.objects.bulk_create(entries)
            ChartOfAccounts.objects.filter(pk=obe.pk).update(balance=F('balance') - obe_delta)

        verb = 'Would post' if dry_run else 'Posted'
        self.stdout.write(self.style.SUCCESS(
            f'\n{verb} outstanding AP for {len(posted)} invoice(s) '
            f'(skipped {skipped} already backfilled):'))
        for internal_id, name, due in posted:
            self.stdout.write(f'  {internal_id} — {name}: {due}')
