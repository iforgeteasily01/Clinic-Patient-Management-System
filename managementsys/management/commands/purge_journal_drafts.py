"""Delete expired journal preview drafts and stale committed staging rows.

``journal_preview.purge_expired_drafts`` also runs at the start of every
preview, so an actively used install never needs this. It exists for installs
that go quiet — a draft staged on Friday should not still be sitting in the
table on Monday waiting to be committed against week-old documents.

Suitable for a daily scheduled task:

    python manage.py purge_journal_drafts
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from managementsys.models import JournalStagingBatch
from managementsys.services.journal_preview import (
    COMMITTED_RETENTION, purge_expired_drafts,
)


class Command(BaseCommand):
    help = 'Purge expired journal preview drafts and old committed staging rows.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be deleted without deleting it.',
        )

    def handle(self, *args, **options):
        now = timezone.now()
        expired = JournalStagingBatch.objects.filter(
            status__in=['draft', 'failed'], expires_at__lt=now,
        )
        retired = JournalStagingBatch.objects.filter(
            status__in=['committed', 'discarded'],
            created_at__lt=now - COMMITTED_RETENTION,
        )

        expired_count, retired_count = expired.count(), retired.count()

        if options['dry_run']:
            self.stdout.write(
                f'[dry-run] {expired_count} draf kedaluwarsa, '
                f'{retired_count} batch lama akan dihapus.'
            )
            return

        purge_expired_drafts()
        self.stdout.write(self.style.SUCCESS(
            f'Dihapus: {expired_count} draf kedaluwarsa, {retired_count} batch lama.'
        ))
