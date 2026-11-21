from django.core.management.base import BaseCommand

from managementsys.models import InventoryItem, Treatment, TreatmentCategory

ACCOUNT_FIELDS = ('revenue_account',)


class Command(BaseCommand):
    help = ('Generate TreatmentCategory records from in-use treatment categories, '
            'provision their GL accounts, and link treatment catalog items.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Show what would change without writing to the database.',
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        if dry_run:
            self.stdout.write(self.style.WARNING('DRY RUN — no changes will be saved.\n'))

        existing = {c.name.strip().lower(): c for c in TreatmentCategory.objects.all()}

        in_use = (
            Treatment.objects
            .exclude(category__isnull=True).exclude(category='')
            .values_list('category', flat=True).distinct()
        )
        created_categories = []
        for raw in in_use:
            name = (raw or '').strip()
            if not name or name.lower() in existing:
                continue
            created_categories.append(name)
            if not dry_run:
                existing[name.lower()] = TreatmentCategory.objects.create(name=name)

        provisioned_accounts = []
        for cat in TreatmentCategory.objects.select_related(*ACCOUNT_FIELDS).order_by('name'):
            missing = [f for f in ACCOUNT_FIELDS if getattr(cat, f'{f}_id') is None]
            if not missing:
                continue
            provisioned_accounts.append((cat.name, missing))
            if not dry_run:
                cat.ensure_accounts()

        linked = []
        for treatment in Treatment.objects.select_related('catalog_item').all():
            if not treatment.catalog_item_id:
                continue
            name = (treatment.category or '').strip()
            if not name:
                continue
            cat = existing.get(name.lower())
            if cat is None:
                continue
            if treatment.catalog_item.item_category_id != cat.id:
                linked.append(treatment.name)
                if not dry_run:
                    InventoryItem.objects.filter(pk=treatment.catalog_item_id).update(
                        item_category=cat
                    )

        verb = 'Would generate' if dry_run else 'Generated'
        if created_categories:
            self.stdout.write(self.style.SUCCESS(
                f'\n{verb} {len(created_categories)} new category(ies):'))
            for name in sorted(created_categories):
                self.stdout.write(f'  + {name}')
        else:
            self.stdout.write('\nNo new categories needed.')

        verb = 'Would provision' if dry_run else 'Provisioned'
        if provisioned_accounts:
            self.stdout.write(self.style.SUCCESS(
                f'\n{verb} GL accounts for {len(provisioned_accounts)} category(ies):'))
            for name, missing in sorted(provisioned_accounts):
                self.stdout.write(f'  {name}: {", ".join(missing)}')
        else:
            self.stdout.write('All categories already have their accounts.')

        verb = 'Would link' if dry_run else 'Linked'
        if linked:
            self.stdout.write(self.style.SUCCESS(
                f'\n{verb} {len(linked)} treatment item(s) to their category.'))
        else:
            self.stdout.write('All treatment items already linked.')

        self.stdout.write(self.style.SUCCESS('\nAvailable treatment categories:'))
        for cat in TreatmentCategory.objects.select_related(*ACCOUNT_FIELDS).order_by('name'):
            rev = cat.revenue_account.account_number if cat.revenue_account_id else '—'
            self.stdout.write(f'  {cat.name}: revenue {rev}')
