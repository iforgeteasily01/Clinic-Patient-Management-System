from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0055_purchase_two_table_redesign'),
    ]

    operations = [
        migrations.AddField(
            model_name='medrec',
            name='status',
            field=models.CharField(
                choices=[('draft', 'Draft'), ('finalized', 'Finalized')],
                default='finalized',
                max_length=10,
            ),
        ),
    ]
