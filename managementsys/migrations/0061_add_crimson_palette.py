from django.db import migrations


def add_crimson(apps, schema_editor):
    ColorPalette = apps.get_model('managementsys', 'ColorPalette')
    ColorPalette.objects.create(
        name='Crimson',
        primary_color='#ef4444',
        secondary_color='#f97316',
        background_color='#1a0000',
        is_dark=True,
        sort_order=10,
    )


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0060_refresh_palettes'),
    ]

    operations = [
        migrations.RunPython(add_crimson, migrations.RunPython.noop),
    ]
