from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0051_accounting'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentcategory',
            name='show_to_beautician',
            field=models.BooleanField(default=True),
        ),
    ]
