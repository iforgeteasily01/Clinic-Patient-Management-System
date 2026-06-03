from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('managementsys', '0044_stockoutlog'),
    ]

    operations = [
        migrations.AddField(
            model_name='stockopnamesession',
            name='warehouse',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='managementsys.warehouse',
            ),
        ),
    ]
