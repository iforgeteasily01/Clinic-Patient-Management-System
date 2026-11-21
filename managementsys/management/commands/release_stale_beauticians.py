from django.core.management.base import BaseCommand

from managementsys.models import Beauticians
from managementsys.services.beautician_assignment import busy_beautician_ids


class Command(BaseCommand):
    help = 'Release beauticians marked busy with no patient currently in treatment.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                             help='Report what would change without saving.')

    def handle(self, *args, **opts):
        dry_run = opts['dry_run']
        busy_with_live_session = busy_beautician_ids()
        stuck = Beauticians.objects.filter(available=False).exclude(
            id__in=busy_with_live_session)

        count = stuck.count()
        if count == 0:
            self.stdout.write('No stuck beauticians found.')
            return

        for beautician in stuck:
            self.stdout.write(f'  releasing #{beautician.id} {beautician.beautician_name}')

        if dry_run:
            self.stdout.write(f'{count} beautician(s) would be released (dry run).')
            return

        stuck.update(available=True)
        self.stdout.write(self.style.SUCCESS(f'{count} beautician(s) released.'))
