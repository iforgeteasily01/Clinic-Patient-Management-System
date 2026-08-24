"""Free any beautician stuck at available=False with no patient actually in
treatment (status 4). Safe to run repeatedly / on a schedule.

`Beauticians.available` is a denormalized flag toggled at session start/end —
see BeauticianAdminStatusSerializer.get_current_session's `is_stuck` logic,
which this mirrors. The relevant write paths now reset it on every way a
session can end, but this sweep is a backstop for anything that reaches the
flag outside those paths (manual DB edits, data imported before this fix, a
future code path that forgets to call `_release_beauticians`).
"""
from django.core.management.base import BaseCommand

from managementsys.models import Beauticians, TreatmentSession


class Command(BaseCommand):
    help = 'Release beauticians marked busy with no patient currently in treatment.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                             help='Report what would change without saving.')

    def handle(self, *args, **opts):
        dry_run = opts['dry_run']
        busy_with_live_session = set(
            TreatmentSession.objects.filter(active_patient__status=4)
            .exclude(beautician__isnull=True)
            .values_list('beautician_id', flat=True)
        )
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
