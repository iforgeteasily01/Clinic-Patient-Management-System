from django.db import migrations

NEW_PALETTES = [
    # name, primary, secondary, background, is_dark, sort_order
    ('Sky',      '#0ea5e9', '#8b5cf6', '#f0f9ff', False, 0),
    ('Sage',     '#059669', '#0891b2', '#ecfdf5', False, 1),
    ('Rose',     '#e11d48', '#f97316', '#fff1f2', False, 2),
    ('Iris',     '#6366f1', '#ec4899', '#eef2ff', False, 3),
    ('Honey',    '#d97706', '#16a34a', '#fffbeb', False, 4),
    ('Abyss',    '#38bdf8', '#a78bfa', '#0f172a', True,  5),
    ('Midnight', '#818cf8', '#34d399', '#1e1b4b', True,  6),
    ('Ember',    '#fb923c', '#f472b6', '#1a0f00', True,  7),
    ('Forest',   '#4ade80', '#22d3ee', '#061a0e', True,  8),
    ('Dusk',     '#f87171', '#fbbf24', '#1a0510', True,  9),
]


def replace_palettes(apps, schema_editor):
    ColorPalette = apps.get_model('managementsys', 'ColorPalette')
    ColorPalette.objects.all().delete()
    for name, primary, secondary, background, is_dark, sort_order in NEW_PALETTES:
        ColorPalette.objects.create(
            name=name,
            primary_color=primary,
            secondary_color=secondary,
            background_color=background,
            is_dark=is_dark,
            sort_order=sort_order,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0059_colorpalette'),
    ]

    operations = [
        migrations.RunPython(replace_palettes, migrations.RunPython.noop),
    ]
