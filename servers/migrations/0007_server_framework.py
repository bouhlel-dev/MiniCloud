from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0006_allow_reuse_of_deleted_server_names"),
    ]

    operations = [
        migrations.AddField(
            model_name="server",
            name="framework",
            field=models.CharField(
                blank=True,
                choices=[
                    ("fastapi", "FastAPI"),
                    ("flask", "Flask"),
                    ("django", "Django"),
                    ("express", "Express"),
                    ("nextjs", "Next.js"),
                ],
                default="",
                max_length=20,
            ),
        ),
    ]
