from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0004_static_site"),
    ]

    operations = [
        migrations.RunSQL(
            "UPDATE servers_server SET image = 'nginx' WHERE image = 'static_site'",
            migrations.RunSQL.noop,
        ),
        migrations.RemoveField(
            model_name="server",
            name="site_content",
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
                    ("custom", "Custom"),
                ],
                max_length=20,
            ),
        ),
    ]
