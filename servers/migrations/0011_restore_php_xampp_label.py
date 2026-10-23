from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0010_rename_php_server_image"),
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
