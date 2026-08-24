"""Seed the default branch and adopt every pre-existing document into it.

Before this migration the database described one clinic implicitly. After it
that clinic is a row, and every historic Invoice, MedRec, ledger line and
warehouse points at it — so a branch-filtered report over historic data returns
the same numbers it did before multi-branch existed, instead of an empty set.

The branch identity is lifted from SiteConfig, which is where the single
clinic's name and address already lived. Nothing is invented.

Backfill is chunked with ``update()`` per model rather than a per-row save loop:
the ledger table is the large one, and ``update()`` skips both signals and the
Vercel push, neither of which should fire for a schema backfill.
"""
from django.db import migrations


# Every model that gained a plain ``branch`` FK in 0112. AppUser is handled
# separately because its field is named home_branch and staff assignment is an
# admin decision, not something a migration should guess at.
BRANCHED_MODELS = [
    'AccountTransfer', 'ActivePatient', 'Appointment', 'AppointmentLocation',
    'Expense', 'Invoice', 'JournalEntry', 'LedgerEntry', 'MedRec',
    'PurchaseInvoice', 'StockOutLog', 'TreatmentSession', 'Warehouse',
]


def seed(apps, schema_editor):
    Branch = apps.get_model('managementsys', 'Branch')
    if Branch.objects.exists():
        return

    SiteConfig = apps.get_model('managementsys', 'SiteConfig')
    cfg = SiteConfig.objects.filter(pk=1).first()

    branch = Branch.objects.create(
        code='PUSAT',
        name=(getattr(cfg, 'clinic_name', '') or 'Klinik Pusat').strip()[:100],
        address_line1=getattr(cfg, 'address_line1', '') or '',
        address_line2=getattr(cfg, 'address_line2', '') or '',
        phone_fax=getattr(cfg, 'phone_fax', '') or '',
        is_active=True,
        is_default=True,
        sort_order=0,
    )

    for model_name in BRANCHED_MODELS:
        model = apps.get_model('managementsys', model_name)
        model.objects.filter(branch__isnull=True).update(branch=branch)

    # Existing staff all worked at the one clinic that existed.
    AppUser = apps.get_model('managementsys', 'AppUser')
    AppUser.objects.filter(home_branch__isnull=True).update(home_branch=branch)


def unseed(apps, schema_editor):
    """Detach, then delete — the FKs are PROTECT, so order matters."""
    Branch = apps.get_model('managementsys', 'Branch')
    for model_name in BRANCHED_MODELS:
        apps.get_model('managementsys', model_name).objects.update(branch=None)
    apps.get_model('managementsys', 'AppUser').objects.update(home_branch=None)
    Branch.objects.filter(code='PUSAT').delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0112_branch_accounttransfer_branch_activepatient_branch_and_more'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
