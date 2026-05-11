from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0020_treatment_catalog_item'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentcategory',
            name='revenue_account',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='treatment_category',
                to='managementsys.chartofaccounts',
            ),
        ),
    ]
