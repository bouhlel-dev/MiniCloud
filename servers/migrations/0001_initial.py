import django.core.validators
from decimal import Decimal

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ServerTemplate",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, unique=True)),
                ("cpu_limit", models.DecimalField(decimal_places=2, max_digits=6, validators=[django.core.validators.MinValueValidator(Decimal("0.01"))])),
                ("memory_mb", models.PositiveIntegerField(validators=[django.core.validators.MinValueValidator(1)])),
                ("description", models.TextField(blank=True)),
            ],
            options={"ordering": ["cpu_limit", "memory_mb"]},
        ),
        migrations.CreateModel(
            name="Server",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100)),
                ("image", models.CharField(choices=[("nginx", "Nginx"), ("python", "Python"), ("node", "Node.js"), ("postgres", "PostgreSQL"), ("custom", "Custom")], max_length=20)),
                ("host_port", models.PositiveIntegerField(blank=True, null=True, unique=True, validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(65535)])),
                ("env_vars", models.JSONField(blank=True, default=dict)),
                ("container_id", models.CharField(blank=True, max_length=64)),
                ("status", models.CharField(choices=[("creating", "Creating"), ("running", "Running"), ("stopped", "Stopped"), ("failed", "Failed"), ("deleted", "Deleted")], default="creating", max_length=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("owner", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="servers", to=settings.AUTH_USER_MODEL)),
                ("template", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="servers", to="servers.servertemplate")),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddConstraint(
            model_name="server",
            constraint=models.UniqueConstraint(fields=("owner", "name"), name="unique_server_name_per_owner"),
        ),
        migrations.AddIndex(
            model_name="server",
            index=models.Index(fields=["owner", "status"], name="servers_ser_owner_i_3fdc65_idx"),
        ),
        migrations.AddIndex(
            model_name="server",
            index=models.Index(fields=["created_at"], name="servers_ser_created_ee67df_idx"),
        ),
    ]
