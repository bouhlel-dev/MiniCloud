from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0008_server_database_size_mb"),
    ]

    operations = [
        migrations.AlterField(
            model_name="server",
            name="image",
            field=models.CharField(
                choices=[
                    ("nginx", "Nginx"),
                    ("python", "Python"),
                    ("node", "Node.js"),
                    ("php", "PHP / XAMPP"),
                    ("postgres", "PostgreSQL"),
                    ("custom", "Custom"),
                ],
                max_length=20,
            ),
        ),
    ]
