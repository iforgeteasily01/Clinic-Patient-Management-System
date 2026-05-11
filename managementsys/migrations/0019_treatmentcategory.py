from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0018_appuser_profile_picture'),
    ]

    operations = [
        migrations.CreateModel(
            name='TreatmentCategory',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=50, unique=True)),
            ],
            options={
                'verbose_name_plural': 'Treatment Categories',
                'ordering': ['name'],
            },
        ),
    ]
