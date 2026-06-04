import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0045_stockopnamesession_warehouse'),
    ]

    operations = [
        migrations.AddField(
            model_name='treatmentcategory',
            name='expense_account',
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='expense_category',
                to='managementsys.chartofaccounts',
            ),
        ),
    ]
