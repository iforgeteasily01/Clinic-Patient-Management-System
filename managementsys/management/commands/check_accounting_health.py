from django.core.management.base import BaseCommand

from managementsys.accounting_checks import (
    DOCUMENT_FIELDS, GO_LIVE, balance_drift, inventory_tie,
    invoices_missing_from_ledger, non_reconciling_invoices, trial_balance,
    unbalanced_documents,
)


class Command(BaseCommand):
    help = 'Check general-ledger integrity. Exits 1 if any hard check fails.'

    def add_arguments(self, parser):
        parser.add_argument('--cutoff', default=GO_LIVE.isoformat(),
                            help=f'Accounting go-live date (default {GO_LIVE.isoformat()}). '
                                 'Invoices before it are excluded by design.')
        parser.add_argument('--verbose-detail', action='store_true',
                            help='List the offending records, not just counts.')

    def handle(self, *args, **opts):
        from datetime import date
        cutoff = date.fromisoformat(opts['cutoff'])
        detail = opts['verbose_detail']
        w = self.stdout.write
        failures = []

        w(self.style.MIGRATE_HEADING('Accounting health check'))

        dr, cr = trial_balance()
        ok = (dr == cr)
        self._line(w, 'trial balance', ok, f'{dr - cr:,.2f}')
        if not ok:
            failures.append(f'trial balance off by {dr - cr:,.2f}')

        for field in DOCUMENT_FIELDS:
            bad = unbalanced_documents(field)
            self._line(w, f'per-document: {field}', not bad, f'{len(bad):,} unbalanced')
            if bad:
                failures.append(f'{len(bad)} unbalanced {field} documents')
                if detail:
                    w(f'      ids: {sorted(bad)[:40]}')

        drift = balance_drift()
        self._line(w, 'stored balances', not drift, f'{len(drift):,} adrift')
        if drift:
            failures.append(f'{len(drift)} account balances disagree with the ledger')
            for account, stored, derived in (drift if detail else drift[:5]):
                w(f'      {account.account_number} {account.name[:34]:<34} '
                  f'stored={stored:>16,.2f} ledger={derived:>16,.2f}')

        gl, on_hand, diff = inventory_tie()
        self._line(w, 'inventory vs subledger', diff == 0, f'{diff:,.2f}')
        if diff:
            failures.append(f'inventory control account off by {diff:,.2f}')
            w(f'      ledger {gl:,.2f}   stock on hand {on_hand:,.2f}')

        missing = invoices_missing_from_ledger(cutoff)
        count = missing.count()
        self._line(w, f'postings since {cutoff}', count == 0, f'{count:,} missing')
        if count:
            total = sum((i.grand_total for i in missing), 0)
            failures.append(f'{count} live invoices since {cutoff} have no ledger rows')
            w(f'      value {total:,.2f}')
            if detail:
                for invoice in missing[:40]:
                    w(f'      {invoice.invoice_number} {invoice.datetime:%Y-%m-%d} '
                      f'{invoice.grand_total:,.2f}')

        odd = non_reconciling_invoices(cutoff)
        if odd:
            total = sum(gap for _inv, gap in odd)
            w(self.style.WARNING(
                f'  WARN  non-reconciling invoices : {len(odd):,} '
                f'(lines vs grand_total differ by {total:,.2f})'))
            w('        absorbed into 4100000 Sales Discount, so the ledger stays balanced;')
            w('        indicates a client posting line prices that disagree with its total.')
            if detail:
                for invoice, gap in odd[:40]:
                    w(f'        {invoice.invoice_number} {gap:,.2f}')
        else:
            self._line(w, 'invoice line reconciliation', True, 'all reconcile')

        if failures:
            w('')
            for problem in failures:
                w(self.style.ERROR(f'  FAIL  {problem}'))
            w(self.style.ERROR(f'\n{len(failures)} check(s) failed. '
                               'Run `manage.py repair_accounting_balance` to fix.'))
            raise SystemExit(1)

        w(self.style.SUCCESS('\nAll checks passed.'))

    def _line(self, w, label, ok, value):
        mark = self.style.SUCCESS('OK  ') if ok else self.style.ERROR('FAIL')
        w(f'  {mark}  {label:<28}: {value}')
