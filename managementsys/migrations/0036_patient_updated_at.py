from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0035_stockopnamesession_stockopnameitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='patient',
            name='updated_at',
            field=models.DateTimeField(auto_now=True),
        ),
    ]
