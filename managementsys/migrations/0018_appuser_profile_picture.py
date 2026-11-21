from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('managementsys', '0017_appuser_roles'),
    ]

    operations = [
        migrations.AddField(
            model_name='appuser',
            name='profile_picture',
            field=models.ImageField(blank=True, null=True, upload_to='profile_pictures/'),
        ),
    ]
