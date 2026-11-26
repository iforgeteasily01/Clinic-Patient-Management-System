"""Take a stock snapshot from the command line — the unattended way in.

There is no background worker, so a daily snapshot is a scheduled task, e.g.
Windows Task Scheduler at 23:30 running:

    Scripts\\python.exe manage.py take_stock_snapshot --once-per-day

``--once-per-day`` skips when an automatic snapshot already exists for today
(Jakarta), so a task that fires twice, or is re-run by hand, keeps one row.
"""
from django.core.management.base import BaseCommand
from django.utils import timezone

from managementsys.models import JAKARTA_TZ, StockSnapshot
from managementsys.services.stock_snapshot import capture, reconciliation, take_snapshot


class Command(BaseCommand):
    help = 'Freeze every item\'s stock quantity and value, with the GL inventory balance.'

    def add_arguments(self, parser):
        parser.add_argument('--once-per-day', action='store_true',
                            help='Skip if an automatic snapshot already exists for today.')
        parser.add_argument('--notes', default='', help='Free-text note stored on the snapshot.')
        parser.add_argument('--dry-run', action='store_true',
                            help='Print the figures without saving anything.')

    def handle(self, *args, **opts):
        today = timezone.now().astimezone(JAKARTA_TZ).date()
        if opts['once_per_day'] and StockSnapshot.objects.filter(
                snapshot_date=today, source=StockSnapshot.SOURCE_AUTO).exists():
            self.stdout.write(f'Automatic snapshot for {today} already exists; skipped.')
            return

        if opts['dry_run']:
            header, _lines = capture()
            snap = StockSnapshot(**header)
        else:
            snap = take_snapshot(source=StockSnapshot.SOURCE_AUTO, notes=opts['notes'])

        rec = reconciliation(snap)
        self.stdout.write(f'Items: {snap.item_count} ({snap.items_in_stock} in stock)')
        self.stdout.write(f'Stock value (batches): {snap.total_value:,.2f}')
        if rec['gl_balance'] is None:
            self.stdout.write('GL inventory: no ledger rows')
        else:
            self.stdout.write(f'GL inventory (1300000): {rec["gl_balance"]:,.2f}')
            self.stdout.write(f'Unexplained difference: {rec["unexplained_difference"]:,.2f}')
        if opts['dry_run']:
            self.stdout.write('Dry run: nothing saved.')
        else:
            self.stdout.write(self.style.SUCCESS(f'Snapshot #{snap.id} saved for {snap.snapshot_date}.'))
