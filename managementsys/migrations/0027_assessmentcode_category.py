from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0026_invoice_payment_method_fk'),
    ]

    operations = [
        migrations.AddField(
            model_name='assessmentcode',
            name='category',
            field=models.IntegerField(
                choices=[(1, 'Common'), (2, 'Uncommon')],
                default=1,
            ),
        ),
        migrations.AlterModelOptions(
            name='assessmentcode',
            options={'ordering': ['category', 'code']},
        ),
    ]
