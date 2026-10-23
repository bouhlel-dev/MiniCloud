import django.core.validators
from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion
import django.db.models.manager
import django.db.models.query_utils
import django.utils.timezone


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("servers", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="MetricSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("cpu_percent", models.DecimalField(decimal_places=2, max_digits=6, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))])),
                ("memory_mb", models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(Decimal("0.00"))])),
                ("net_rx", models.BigIntegerField(validators=[django.core.validators.MinValueValidator(0)])),
                ("net_tx", models.BigIntegerField(validators=[django.core.validators.MinValueValidator(0)])),
                ("timestamp", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("server", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="metric_snapshots", to="servers.server")),
            ],
            options={"ordering": ["-timestamp"]},
        ),
        migrations.CreateModel(
            name="HealthCheck",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("auto_restart_attempts", models.PositiveSmallIntegerField(default=0)),
                ("last_checked_at", models.DateTimeField(blank=True, null=True)),
                ("last_container_status", models.CharField(blank=True, max_length=50)),
                ("last_error", models.TextField(blank=True)),
                ("last_alert_sent_at", models.DateTimeField(blank=True, null=True)),
                ("server", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="health_check", to="servers.server")),
            ],
        ),
        migrations.CreateModel(
            name="Incident",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("type", models.CharField(choices=[("down", "Down"), ("high_cpu", "High CPU"), ("high_memory", "High memory")], max_length=20)),
                ("status", models.CharField(choices=[("open", "Open"), ("resolved", "Resolved")], default="open", max_length=10)),
                ("opened_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                ("server", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="incidents", to="servers.server")),
            ],
            options={"ordering": ["-opened_at"]},
        ),
        migrations.AddIndex(
            model_name="metricsnapshot",
            index=models.Index(fields=["server", "timestamp"], name="monitoring__server__529872_idx"),
        ),
        migrations.AddConstraint(
            model_name="incident",
            constraint=models.UniqueConstraint(condition=models.Q(("status", "open")), fields=("server", "type"), name="unique_open_incident_per_type"),
        ),
        migrations.AddIndex(
            model_name="incident",
            index=models.Index(fields=["server", "status"], name="monitoring__server__f2d785_idx"),
        ),
    ]
