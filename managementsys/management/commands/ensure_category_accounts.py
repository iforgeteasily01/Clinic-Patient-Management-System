"""
ensure_category_accounts
========================
Backfills missing revenue, COGS, and expense GL accounts for every
TreatmentCategory (which is also the item_category used by InventoryItem).

Usage:
    python manage.py ensure_category_accounts
    python manage.py ensure_category_accounts --dry-run
"""

from django.core.management.base import BaseCommand
from django.db.models import Max

from managementsys.models import ChartOfAccounts, TreatmentCategory


def _next_account_number(range_min: int, range_max: int, step: int = 1000) -> int:
    max_num = (
        ChartOfAccounts.objects
        .filter(account_number__gte=range_min, account_number__lte=range_max)
        .aggregate(m=Max('account_number'))['m']
    )
    nxt = (max_num + step) if max_num is not None else range_min
    if nxt > range_max:
        raise ValueError(f'Account number range {range_min}–{range_max} exhausted.')
    return nxt


def _ensure_account(category, field_name, range_min, range_max, account_type, label_prefix,
                    dry_run, created, already_exist):
    acct = getattr(category, field_name)
    if acct is not None:
        already_exist.append(f'  {category.name}: {field_name} → {acct.account_number} {acct.name}')
        return

    account_number = _next_account_number(range_min, range_max)
    name = f'{label_prefix} – {category.name}'
    created.append(f'  {category.name}: {field_name} → {account_number} {name}')

    if not dry_run:
        acct = ChartOfAccounts.objects.create(
            account_number=account_number,
            name=name,
            account_type=account_type,
        )
        setattr(category, field_name, acct)
        category.save(update_fields=[field_name])


class Command(BaseCommand):
    help = 'Ensure every TreatmentCategory has revenue, COGS, and expense GL accounts.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be created without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.\n'))

        categories = TreatmentCategory.objects.select_related(
            'revenue_account', 'cogs_account', 'expense_account',
        ).order_by('name')

        if not categories.exists():
            self.stdout.write('No treatment categories found.')
            return

        created = []
        already_exist = []

        for cat in categories:
            _ensure_account(cat, 'revenue_account', 4400000, 4999999,
                            'revenue', 'Treatment Revenue', dry_run, created, already_exist)
            _ensure_account(cat, 'cogs_account', 5400000, 5999999,
                            'cogs', 'COGS', dry_run, created, already_exist)
            _ensure_account(cat, 'expense_account', 6900000, 6999999,
                            'expense', 'Expense', dry_run, created, already_exist)

        if already_exist:
            self.stdout.write(self.style.SUCCESS(f'Already linked ({len(already_exist)}):'))
            for line in already_exist:
                self.stdout.write(line)

        if created:
            verb = 'Would create' if dry_run else 'Created'
            self.stdout.write(self.style.SUCCESS(f'\n{verb} ({len(created)}):'))
            for line in created:
                self.stdout.write(line)
        else:
            self.stdout.write('\nAll accounts already present — nothing to create.')
