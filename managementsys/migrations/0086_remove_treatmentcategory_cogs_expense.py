"""Phase 3 step 2/2: drop TreatmentCategory.cogs_account / expense_account.

Must run after 0085 (which nulled both fields) so removing the columns never
has to contend with a live FK value.
"""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0085_null_treatmentcategory_cogs_expense'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='treatmentcategory',
            name='cogs_account',
        ),
        migrations.RemoveField(
            model_name='treatmentcategory',
            name='expense_account',
        ),
    ]
