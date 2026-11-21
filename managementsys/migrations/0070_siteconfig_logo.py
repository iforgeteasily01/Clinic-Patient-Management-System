from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0069_backfill_treatment_item_category'),
    ]

    operations = [
        migrations.AddField(
            model_name='siteconfig',
            name='logo',
            field=models.ImageField(blank=True, null=True, upload_to='site/'),
        ),
    ]
