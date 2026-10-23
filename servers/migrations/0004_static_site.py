from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0003_release_deleted_server_ports"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="site_content",
            field=models.TextField(blank=True),
        ),
        migrations.AlterField(
            model_name="server",
            name="image",
            field=models.CharField(
                choices=[
                    ("nginx", "Nginx"),
                    ("python", "Python"),
                    ("node", "Node.js"),
                    ("postgres", "PostgreSQL"),
                    ("static_site", "Static Website"),
                    ("custom", "Custom"),
                ],
                max_length=20,
            ),
        ),
    ]
