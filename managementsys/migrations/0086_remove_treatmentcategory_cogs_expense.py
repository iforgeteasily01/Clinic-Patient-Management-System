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
