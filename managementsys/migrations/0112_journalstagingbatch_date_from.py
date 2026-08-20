from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0111_patient_wa_opt_in_patient_wa_opt_in_at_whatsappblast_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='journalstagingbatch',
            name='date_from',
            field=models.DateField(blank=True, null=True),
        ),
    ]
