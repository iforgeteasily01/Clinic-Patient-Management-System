from django.db import migrations

SEED_LOCATIONS = [
    ('Ruang Konsultasi 1', 'K1'),
    ('Ruang Konsultasi 2', 'K2'),
    ('Ruang Perawatan 1', 'P1'),
    ('Ruang Perawatan 2', 'P2'),
]


def seed_locations(apps, schema_editor):
    AppointmentLocation = apps.get_model('managementsys', 'AppointmentLocation')
    AppointmentLocation.objects.bulk_create([
        AppointmentLocation(name=name, room_code=code, is_active=True)
        for name, code in SEED_LOCATIONS
    ])


def unseed_locations(apps, schema_editor):
    AppointmentLocation = apps.get_model('managementsys', 'AppointmentLocation')
    AppointmentLocation.objects.filter(
        room_code__in=[code for _, code in SEED_LOCATIONS]
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0067_appointmentlocation_doctors_ihs_id_doctors_nik_and_more'),
    ]

    operations = [
        migrations.RunPython(seed_locations, unseed_locations),
    ]
