from decimal import Decimal

from django.conf import settings
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
    consecutive_failures = models.PositiveSmallIntegerField(default=0)
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
            return False
        self.status = self.Status.RESOLVED
        self.resolved_at = timezone.now()
        self.save(update_fields=["status", "resolved_at"])
        return True


class Notification(models.Model):
    class Kind(models.TextChoices):
        INCIDENT_OPENED = "incident_opened", "Incident opened"
        INCIDENT_RESOLVED = "incident_resolved", "Incident resolved"
        SYSTEM = "system", "System"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    server = models.ForeignKey(
        Server,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    kind = models.CharField(max_length=32, choices=Kind.choices, default=Kind.SYSTEM)
    title = models.CharField(max_length=140, blank=True, default="")
    message = models.TextField()
    level = models.CharField(
        max_length=20,
        choices=[
            ("info", "Info"),
            ("warning", "Warning"),
            ("error", "Error"),
            ("success", "Success"),
        ],
        default="info",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read", "created_at"])]

