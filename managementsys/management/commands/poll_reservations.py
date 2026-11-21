import time

from django.core.management.base import BaseCommand

from managementsys.services.reservation_sync import ReservationSyncError, run_once


class Command(BaseCommand):
    help = 'Pull new online reservations from the Vercel booking form.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--loop', action='store_true',
            help='Keep polling instead of exiting after one pass.',
        )
        parser.add_argument(
            '--interval', type=int, default=60,
            help='Seconds between passes in --loop mode (default 60).',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Maximum reservations to collect in one pass.',
        )

    def handle(self, *args, **options):
        interval = max(5, options['interval'])
        limit = options['limit']

        if not options['loop']:
            self._pass(limit)
            return

        self.stdout.write(
            self.style.NOTICE('Polling for online reservations every %ds. Ctrl-C to stop.' % interval)
        )
        while True:
            try:
                while self._pass(limit):
                    pass
            except KeyboardInterrupt:
                self.stdout.write(self.style.NOTICE('Stopped.'))
                return
            time.sleep(interval)

    def _pass(self, limit):
        try:
            result = run_once(limit)
        except ReservationSyncError as exc:
            self.stderr.write(self.style.WARNING('[reservations] %s' % exc))
            return False

        if result['imported']:
            self.stdout.write(self.style.SUCCESS(
                '[reservations] imported %d new booking(s)' % result['imported']
            ))
        if result['duplicates']:
            self.stdout.write(
                '[reservations] %d already imported (redelivery)' % result['duplicates']
            )
        for failure in result['failed']:
            self.stderr.write(self.style.ERROR(
                '[reservations] row %s failed: %s' % (failure['id'], failure['error'])
            ))

        return bool(result['more'])
