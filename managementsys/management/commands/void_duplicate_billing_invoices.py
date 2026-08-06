"""Void billing-queue invoices that re-bill a visit the POS had already invoiced.

The POS creates an invoice via /api/invoices/create/ and then clears the queue entry.
When the caller omitted ``?skip_invoice=1`` the billing endpoint billed the same
treatments again, producing a second invoice with no cashier and no payment method.
The patient paid once, so the second invoice is a phantom that inflates revenue and
the patient's recorded spend.

Scope is deliberately narrow, because voiding a real sale is worse than leaving a
phantom in place:

* Only invoices ``repair_accounting_balance`` routed to the Undeposited Funds
  clearing account are considered. That is the exact signature of the billing queue:
  no cashier, no warehouse, no payment method of its own. iPos-imported rows are
  never candidates — they are not in the ledger and are not queue output.
* A candidate is matched only against an invoice that is **not itself a candidate**
  and that carries a real payment method, so the two halves of a pair can never void
  each other.
* Matching is 1:1 and greedy by time. A paid invoice can absorb at most one
  duplicate, so two phantoms against a single payment leave the second one standing
  for a human to look at.
* Every line must match on name, quantity and price. Partial overlap is reported,
  never voided.

Read-only by default; pass --apply to commit.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from managementsys.accounting_checks import GO_LIVE, IMPORTED_INVOICE_PREFIX
from managementsys.models import AuditLog, Invoice, LedgerEntry
from managementsys.views.crm_page import refresh_crm_profile
from managementsys.services.journal_engine import ACC_UNDEPOSITED

D = Decimal


def line_signature(invoice):
    """(name, quantity, price) per line, normalised across both checkout paths.

    The POS writes ``item_name`` with no FK; the billing queue writes ``item_id``
    with a blank name. Comparing on the resolved display name is what makes the
    same treatment recognisable from either side.
    """
    return {
        (((item.item.name if item.item_id else item.item_name) or '').strip().lower(),
         item.quantity, item.price)
        for item in invoice.items.all()
    }


class Command(BaseCommand):
    help = 'Void phantom billing-queue invoices that duplicate an already-paid invoice.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Commit. Without it the work is rolled back after reporting.')
        parser.add_argument('--since', default=GO_LIVE.isoformat(),
                            help=f'Only consider invoices on/after this date '
                                 f'(default {GO_LIVE.isoformat()}).')

    def handle(self, *args, **opts):
        since = date.fromisoformat(opts['since'])
        apply_changes = opts['apply']
        w = self.stdout.write

        w(self.style.MIGRATE_HEADING(
            'Voiding duplicate billing-queue invoices'
            if apply_changes else
            'Duplicate billing-queue invoices — DRY RUN (rolled back at the end)'
        ))

        candidates = list(
            Invoice.objects
            .filter(datetime__date__gte=since, is_voided=False,
                    payment_method__linked_account__account_number=ACC_UNDEPOSITED)
            .exclude(invoice_number__startswith=IMPORTED_INVOICE_PREFIX)
            .prefetch_related('items__item')
            # Tie-break on id. A POS run stamps every invoice of a visit with
            # the same `datetime`, so ordering on that column alone leaves the
            # row order up to PostgreSQL — and because a paid invoice may absorb
            # only one duplicate (`claimed`), the order decides *which* invoice
            # gets voided. That must not be arbitrary for a command that
            # destroys revenue records. id preserves the intended chronology.
            .order_by('datetime', 'id')
        )
        candidate_ids = {inv.id for inv in candidates}

        stats = defaultdict(int)
        stats['voided_amount'] = D('0')
        stats['kept_amount'] = D('0')
        decisions = []
        # A paid invoice may absorb only one duplicate.
        claimed = set()

        with transaction.atomic():
            for invoice in candidates:
                mine = line_signature(invoice)
                if not mine or not invoice.patient_no_id:
                    decisions.append((invoice, None, 'no lines or no patient — skipped'))
                    stats['skipped'] += 1
                    continue

                peers = (
                    Invoice.objects
                    .filter(patient_no_id=invoice.patient_no_id,
                            datetime__date=invoice.datetime.date(),
                            is_voided=False,
                            payment_method__isnull=False)
                    .exclude(id__in=candidate_ids)      # never match another phantom
                    .exclude(id__in=claimed)            # 1:1
                    .prefetch_related('items__item')
                    .order_by('datetime', 'id')         # same tie-break as above
                )

                match = None
                partial = None
                for peer in peers:
                    theirs = line_signature(peer)
                    if mine <= theirs:
                        match = peer
                        break
                    if mine & theirs:
                        partial = peer

                if match is not None:
                    claimed.add(match.id)
                    decisions.append((invoice, match, 'duplicate'))
                    stats['voided'] += 1
                    stats['voided_amount'] += invoice.grand_total
                elif partial is not None:
                    decisions.append((invoice, partial, 'partial overlap — left alone'))
                    stats['partial'] += 1
                    stats['kept_amount'] += invoice.grand_total
                else:
                    decisions.append((invoice, None, 'no counterpart — genuinely unbilled'))
                    stats['unbilled'] += 1
                    stats['kept_amount'] += invoice.grand_total

            touched = set()
            for invoice, match, verdict in decisions:
                if verdict != 'duplicate':
                    continue
                # These invoices carry only the cash/revenue block written by
                # repair_accounting_balance, and no stock ever moved for them
                # (none of the duplicated treatments consume materials), so the
                # rows are removed rather than reversed.
                LedgerEntry.objects.filter(invoice=invoice).delete()
                invoice.is_voided = True
                invoice.voided_at = timezone.now()
                invoice.save(update_fields=['is_voided', 'voided_at'])
                AuditLog.objects.create(
                    action='DELETE',
                    entity_type='Invoice',
                    entity_id=str(invoice.id),
                    description=(
                        f'Invoice {invoice.invoice_number} voided as a duplicate of '
                        f'{match.invoice_number} — the billing queue re-billed a visit '
                        f'the POS had already invoiced'
                    ),
                )
                if invoice.patient_no_id:
                    touched.add(invoice.patient_no)

            for patient in touched:
                refresh_crm_profile(patient)

            self._report(w, decisions, stats)

            if not apply_changes:
                transaction.set_rollback(True)
                w(self.style.WARNING('\nDRY RUN — nothing was written. Re-run with --apply.'))
            else:
                w(self.style.SUCCESS('\nChanges committed.'))

    def _report(self, w, decisions, stats):
        w('')
        for invoice, match, verdict in decisions:
            if verdict == 'duplicate':
                paid_via = match.payment_method.name if match.payment_method_id else '(none)'
                w(f'  VOID  {invoice.invoice_number:<18} {invoice.datetime:%Y-%m-%d} '
                  f'{invoice.grand_total:>12,.2f}  duplicate of {match.invoice_number} '
                  f'({paid_via})')
        for invoice, match, verdict in decisions:
            if verdict == 'duplicate':
                continue
            w(self.style.WARNING(
                f'  KEEP  {invoice.invoice_number:<18} {invoice.datetime:%Y-%m-%d} '
                f'{invoice.grand_total:>12,.2f}  {verdict}'))

        w('')
        w(f'  voided as duplicates         : {stats["voided"]:,}')
        w(f'  revenue removed              : {stats["voided_amount"]:,.2f}')
        w(f'  kept — genuinely unbilled    : {stats["unbilled"]:,}')
        w(f'  kept — partial overlap       : {stats["partial"]:,}')
        w(f'  kept value (needs a decision): {stats["kept_amount"]:,.2f}')
