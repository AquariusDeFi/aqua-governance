from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('governance', '0031_consumed_transaction'),
    ]

    operations = [
        migrations.AddField(
            model_name='proposal',
            name='payment_not_found_since',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
