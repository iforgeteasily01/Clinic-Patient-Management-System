from django.db import migrations


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

    AppUser = apps.get_model('managementsys', 'AppUser')
    AppUser.objects.filter(home_branch__isnull=True).update(home_branch=branch)


def unseed(apps, schema_editor):
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
