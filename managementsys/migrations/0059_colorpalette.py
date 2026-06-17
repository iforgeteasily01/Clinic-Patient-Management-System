import django.utils.timezone
from django.db import migrations, models


SEED_PALETTES = [
    ('Ocean',    '#0284c7', '#7c3aed', '#f1f5f9', False, 0),
    ('Forest',   '#16a34a', '#0d9488', '#f0fdf4', False, 1),
    ('Sunset',   '#ea580c', '#db2777', '#fff7ed', False, 2),
    ('Rose',     '#e11d48', '#9333ea', '#fff1f2', False, 3),
    ('Midnight', '#6366f1', '#06b6d4', '#eef2ff', False, 4),
    ('Amber',    '#d97706', '#059669', '#fffbeb', False, 5),
    ('Slate',    '#475569', '#0ea5e9', '#f8fafc', False, 6),
    ('Dark',     '#38bdf8', '#a78bfa', '#0f172a', True,  7),
    ('Noir',     '#f472b6', '#34d399', '#09090b', True,  8),
]


def seed_palettes(apps, schema_editor):
    ColorPalette = apps.get_model('managementsys', 'ColorPalette')
    for name, primary, secondary, background, is_dark, sort_order in SEED_PALETTES:
        ColorPalette.objects.create(
            name=name,
            primary_color=primary,
            secondary_color=secondary,
            background_color=background,
            is_dark=is_dark,
            sort_order=sort_order,
        )


def unseed_palettes(apps, schema_editor):
    ColorPalette = apps.get_model('managementsys', 'ColorPalette')
    ColorPalette.objects.filter(name__in=[p[0] for p in SEED_PALETTES]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0058_productionrecipe_productionrun_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='ColorPalette',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50)),
                ('primary_color', models.CharField(max_length=7)),
                ('secondary_color', models.CharField(max_length=7)),
                ('background_color', models.CharField(max_length=7)),
                ('is_dark', models.BooleanField(default=False)),
                ('sort_order', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
            ],
            options={
                'ordering': ['sort_order', 'id'],
            },
        ),
        migrations.RunPython(seed_palettes, reverse_code=unseed_palettes),
    ]
