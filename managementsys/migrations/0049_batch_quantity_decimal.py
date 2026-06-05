from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0048_treatmentmaterial'),
    ]

    operations = [
        migrations.AlterField(
            model_name='inventorybatch',
            name='quantity_initial',
            field=models.DecimalField(decimal_places=4, max_digits=14),
        ),
        migrations.AlterField(
            model_name='inventorybatch',
            name='quantity_remaining',
            field=models.DecimalField(decimal_places=4, max_digits=14),
        ),
        migrations.AlterField(
            model_name='stockoutlog',
            name='quantity',
            field=models.DecimalField(decimal_places=4, max_digits=14),
        ),
    ]
