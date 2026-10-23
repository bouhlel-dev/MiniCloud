from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0005_remove_static_site"),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="server",
            name="unique_server_name_per_owner",
        ),
        migrations.AddConstraint(
            model_name="server",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "deleted"), _negated=True),
                fields=("owner", "name"),
                name="unique_active_server_name_per_owner",
            ),
        ),
    ]
