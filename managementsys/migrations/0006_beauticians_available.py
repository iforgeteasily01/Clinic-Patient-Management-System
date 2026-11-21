from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0005_treatmentsession'),
    ]

    operations = [
        migrations.AddField(
            model_name='beauticians',
            name='available',
            field=models.BooleanField(default=True),
        ),
    ]
