from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="custom_image",
            field=models.CharField(blank=True, max_length=255),
        ),
    ]

