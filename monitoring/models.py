from decimal import Decimal

from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone

from servers.models import Server


class MetricSnapshot(models.Model):
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="metric_snapshots")
    cpu_percent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    memory_mb = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    net_rx = models.BigIntegerField(validators=[MinValueValidator(0)])
    net_tx = models.BigIntegerField(validators=[MinValueValidator(0)])
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["server", "timestamp"])]


class HealthCheck(models.Model):
    server = models.OneToOneField(Server, on_delete=models.CASCADE, related_name="health_check")
    auto_restart_attempts = models.PositiveSmallIntegerField(default=0)
    last_checked_at = models.DateTimeField(blank=True, null=True)
    last_container_status = models.CharField(max_length=50, blank=True)
    last_error = models.TextField(blank=True)
    last_alert_sent_at = models.DateTimeField(blank=True, null=True)


class Incident(models.Model):
    class Type(models.TextChoices):
        DOWN = "down", "Down"
        HIGH_CPU = "high_cpu", "High CPU"
        HIGH_MEMORY = "high_memory", "High memory"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        RESOLVED = "resolved", "Resolved"

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="incidents")
    type = models.CharField(max_length=20, choices=Type.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    opened_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["server", "type"],
                condition=models.Q(status="open"),
                name="unique_open_incident_per_type",
            ),
        ]
        indexes = [models.Index(fields=["server", "status"])]

    def resolve(self):
        if self.status == self.Status.RESOLVED:
            return
        self.status = self.Status.RESOLVED
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at"])

