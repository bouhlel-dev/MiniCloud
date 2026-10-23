from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0007_server_framework"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="database_size_mb",
            field=models.PositiveIntegerField(
                blank=True,
                choices=[(512, "512 MB"), (1024, "1 GB"), (5120, "5 GB"), (10240, "10 GB")],
                null=True,
            ),
        ),
    ]
