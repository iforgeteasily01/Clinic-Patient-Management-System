from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0062_merge_20260618_0230'),
    ]

    operations = [
        migrations.AddField(
            model_name='medrec',
            name='clinician',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
    ]
