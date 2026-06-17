from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0056_medrec_status'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatment',
            name='sort_order',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='treatmentcategory',
            name='sort_order',
            field=models.IntegerField(default=0),
        ),
        migrations.AlterModelOptions(
            name='treatment',
            options={'ordering': ['sort_order', 'name']},
        ),
        migrations.AlterModelOptions(
            name='treatmentcategory',
            options={'ordering': ['sort_order', 'name'], 'verbose_name_plural': 'Treatment Categories'},
        ),
    ]
