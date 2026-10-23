import logging
from datetime import timedelta
from decimal import Decimal
from math import ceil

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from servers.docker_service import DockerContainerNotFound, DockerServiceError
from servers.models import Server

from .models import HealthCheck, Incident, MetricSnapshot


logger = logging.getLogger(__name__)


def record_metric_snapshot(server, stats, timestamp=None):
    snapshot = MetricSnapshot.objects.create(
        server=server,
        cpu_percent=Decimal(str(stats["cpu_percent"])),
        memory_mb=Decimal(str(stats["memory_mb"])),
        net_rx=int(stats["net_rx"]),
        net_tx=int(stats["net_tx"]),
        timestamp=timestamp or timezone.now(),
    )
    evaluate_resource_incidents(server, snapshot)
    return snapshot


def evaluate_resource_incidents(server, snapshot):
    cpu_threshold = Decimal(str(settings.MINICLOUD_HIGH_CPU_PERCENT))
    memory_threshold = Decimal(str(settings.MINICLOUD_HIGH_MEMORY_PERCENT))
    cpu_sustained = _is_sustained_threshold(server, "cpu_percent", cpu_threshold)
    memory_percent = (snapshot.memory_mb / Decimal(server.template.memory_mb)) * 100
    memory_sustained = _is_sustained_memory_threshold(server, memory_threshold)

    if cpu_sustained:
        open_incident(server, Incident.Type.HIGH_CPU)
    elif snapshot.cpu_percent <= cpu_threshold:
        resolve_incident(server, Incident.Type.HIGH_CPU)

    if memory_sustained:
        open_incident(server, Incident.Type.HIGH_MEMORY)
    elif memory_percent <= memory_threshold:
        resolve_incident(server, Incident.Type.HIGH_MEMORY)


def record_healthy_check(server, container_status):
    health_check, _ = HealthCheck.objects.get_or_create(server=server)
    health_check.auto_restart_attempts = 0
    health_check.last_checked_at = timezone.now()
    health_check.last_container_status = container_status
    health_check.last_error = ""
    health_check.save(
        update_fields=["auto_restart_attempts", "last_checked_at", "last_container_status", "last_error"]
    )
    resolve_incident(server, Incident.Type.DOWN)


def handle_unhealthy_server(server, container_status, docker_service, error_message=""):
    health_check, _ = HealthCheck.objects.get_or_create(server=server)
    now = timezone.now()
    open_incident(server, Incident.Type.DOWN)
    health_check.last_checked_at = now
    health_check.last_container_status = container_status

    if isinstance(error_message, DockerContainerNotFound):
        health_check.last_error = str(error_message)
        health_check.save(update_fields=["last_checked_at", "last_container_status", "last_error"])
        _mark_server_failed(server)
        return

    if health_check.auto_restart_attempts >= settings.MINICLOUD_HEALTH_MAX_RESTART_ATTEMPTS:
        health_check.last_error = "The automatic restart limit has been reached."
        health_check.save(update_fields=["last_checked_at", "last_container_status", "last_error"])
        _mark_server_failed(server)
        return

    health_check.auto_restart_attempts += 1
    try:
        docker_service.start_container(server.container_id)
    except DockerServiceError as error:
        health_check.last_error = str(error)
        if health_check.auto_restart_attempts >= settings.MINICLOUD_HEALTH_MAX_RESTART_ATTEMPTS:
            _mark_server_failed(server)
    else:
        health_check.last_error = ""
        health_check.last_container_status = "restart_requested"
    health_check.save(
        update_fields=[
            "auto_restart_attempts",
            "last_checked_at",
            "last_container_status",
            "last_error",
        ]
    )


def open_incident(server, incident_type):
    try:
        with transaction.atomic():
            incident, created = Incident.objects.get_or_create(
                server=server,
                type=incident_type,
                status=Incident.Status.OPEN,
            )
    except IntegrityError:
        incident = Incident.objects.get(server=server, type=incident_type, status=Incident.Status.OPEN)
        created = False
    if created:
        _send_incident_alert(server, incident)
    return incident, created


def resolve_incident(server, incident_type):
    return Incident.objects.filter(
        server=server,
        type=incident_type,
        status=Incident.Status.OPEN,
    ).update(status=Incident.Status.RESOLVED, resolved_at=timezone.now())


def prune_expired_metric_snapshots():
    cutoff = timezone.now() - timedelta(days=settings.MINICLOUD_METRIC_RETENTION_DAYS)
    deleted, _ = MetricSnapshot.objects.filter(timestamp__lt=cutoff).delete()
    return deleted


def _is_sustained_threshold(server, field_name, threshold):
    window_seconds = 60
    now = timezone.now()
    cutoff = now - timedelta(seconds=window_seconds + settings.MINICLOUD_METRIC_INTERVAL_SECONDS)
    samples = list(
        MetricSnapshot.objects.filter(server=server, timestamp__gte=cutoff)
        .order_by("timestamp")
        .values_list("timestamp", field_name)
    )
    return _has_sustained_breach(samples, lambda value: value > threshold, now)


def _is_sustained_memory_threshold(server, threshold):
    window_seconds = 60
    now = timezone.now()
    cutoff = now - timedelta(seconds=window_seconds + settings.MINICLOUD_METRIC_INTERVAL_SECONDS)
    memory_limit = Decimal(server.template.memory_mb)
    samples = MetricSnapshot.objects.filter(server=server, timestamp__gte=cutoff).order_by("timestamp").values_list(
        "timestamp", "memory_mb"
    )
    return _has_sustained_breach(
        samples,
        lambda value: (value / memory_limit) * 100 > threshold,
        now,
    )


def _has_sustained_breach(samples, is_above_threshold, now):
    required_samples = ceil(60 / settings.MINICLOUD_METRIC_INTERVAL_SECONDS) + 1
    high_streak = []
    for timestamp, value in samples:
        if is_above_threshold(value):
            high_streak.append((timestamp, value))
        else:
            high_streak = []
    return (
        len(high_streak) >= required_samples
        and now - high_streak[0][0] >= timedelta(seconds=60)
    )


def _mark_server_failed(server):
    if server.status != Server.Status.FAILED and server.can_transition_to(Server.Status.FAILED):
        server.transition_to(Server.Status.FAILED)


def _send_incident_alert(server, incident):
    if not server.owner.email:
        return
    health_check, _ = HealthCheck.objects.get_or_create(server=server)
    cooldown_cutoff = timezone.now() - timedelta(seconds=settings.MINICLOUD_ALERT_COOLDOWN_SECONDS)
    if health_check.last_alert_sent_at and health_check.last_alert_sent_at >= cooldown_cutoff:
        return
    try:
        send_mail(
            subject=f"[MiniCloud] {incident.get_type_display()} incident on {server.name}",
            message=(
                f"MiniCloud detected a {incident.get_type_display().lower()} incident on "
                f"server '{server.name}' at {incident.opened_at.isoformat()}."
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[server.owner.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Could not send incident alert for server %s", server.pk)
        return
    health_check.last_alert_sent_at = timezone.now()
    health_check.save(update_fields=["last_alert_sent_at"])
