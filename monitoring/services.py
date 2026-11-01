import logging
from datetime import timedelta
from decimal import Decimal
from math import ceil

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone

from servers.docker_service import DockerContainerNotFound, DockerServiceError
from servers.models import Server

from .models import HealthCheck, Incident, MetricSnapshot, Notification


logger = logging.getLogger(__name__)


def create_notification(user, message, level="info", *, server=None, kind=Notification.Kind.SYSTEM, title=""):
    notification = Notification.objects.create(
        user=user,
        server=server,
        kind=kind,
        title=title,
        message=message,
        level=level,
    )
    channel_layer = get_channel_layer()
    if channel_layer:
        async_to_sync(channel_layer.group_send)(
            f"user_{user.id}_notifications",
            {
                "type": "notification_message",
                "data": {
                    "id": notification.id,
                    "kind": notification.kind,
                    "kind_display": notification.get_kind_display(),
                    "title": notification.title,
                    "message": notification.message,
                    "level": notification.level,
                    "created_at": notification.created_at.isoformat(),
                    "is_read": notification.is_read,
                    "server": notification.server_id,
                    "server_name": notification.server.name if notification.server else None,
                    "server_status": notification.server.status if notification.server else None,
                },
            },
        )
    return notification


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
    cpu_share = _cpu_share(server, snapshot.cpu_percent)
    memory_share = _memory_share(server, snapshot.memory_mb)

    if _is_sustained_breach(server, "cpu_percent", cpu_threshold, lambda value: _cpu_share(server, value)):
        open_incident(server, Incident.Type.HIGH_CPU)
    elif cpu_share <= cpu_threshold:
        resolve_incident(server, Incident.Type.HIGH_CPU)

    if _is_sustained_breach(server, "memory_mb", memory_threshold, lambda value: _memory_share(server, value)):
        open_incident(server, Incident.Type.HIGH_MEMORY)
    elif memory_share <= memory_threshold:
        resolve_incident(server, Incident.Type.HIGH_MEMORY)


def record_healthy_check(server, container_status):
    health_check, _ = HealthCheck.objects.get_or_create(server=server)
    health_check.auto_restart_attempts = 0
    health_check.consecutive_failures = 0
    health_check.last_checked_at = timezone.now()
    health_check.last_container_status = container_status
    health_check.last_error = ""
    health_check.save(
        update_fields=[
            "auto_restart_attempts",
            "consecutive_failures",
            "last_checked_at",
            "last_container_status",
            "last_error",
        ]
    )
    if server.status == Server.Status.FAILED:
        server.transition_to(Server.Status.RUNNING)
    resolve_incident(server, Incident.Type.DOWN)


def handle_unhealthy_server(server, container_status, docker_service, error_message=""):
    down_failures = settings.MINICLOUD_HEALTH_DOWN_FAILURES
    health_check, _ = HealthCheck.objects.get_or_create(server=server)
    now = timezone.now()
    # Capped so a week-long outage cannot overflow the smallint and break the task for every server.
    health_check.consecutive_failures = min(health_check.consecutive_failures + 1, down_failures)
    health_check.last_checked_at = now
    health_check.last_container_status = container_status

    # One failed poll is usually Docker being busy or a container mid-start; only a streak is an outage.
    if health_check.consecutive_failures < down_failures:
        health_check.save(update_fields=["consecutive_failures", "last_checked_at", "last_container_status"])
        return

    if isinstance(error_message, DockerContainerNotFound):
        health_check.last_error = str(error_message)
        health_check.save(
            update_fields=["consecutive_failures", "last_checked_at", "last_container_status", "last_error"]
        )
        open_incident(server, Incident.Type.DOWN)
        _mark_server_failed(server)
        return

    if health_check.auto_restart_attempts >= settings.MINICLOUD_HEALTH_MAX_RESTART_ATTEMPTS:
        health_check.last_error = "The automatic restart limit has been reached."
        health_check.save(
            update_fields=["consecutive_failures", "last_checked_at", "last_container_status", "last_error"]
        )
        open_incident(server, Incident.Type.DOWN)
        _mark_server_failed(server)
        return

    open_incident(server, Incident.Type.DOWN)
    health_check.auto_restart_attempts += 1
    try:
        docker_service.start_container(server.container_id, server=server)
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
            "consecutive_failures",
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
        display = incident.get_type_display()
        if incident_type == Incident.Type.DOWN:
            detail = (
                f"Container {server.name} is unreachable. The health check closes this automatically "
                "once the container reports healthy again."
            )
            level = "error"
        else:
            detail = (
                f"{server.name} has been over the {display.lower()} threshold for a sustained period. "
                "The incident stays open until metrics drop back under the limit."
            )
            level = "warning"
        create_notification(
            server.owner,
            detail,
            level=level,
            server=server,
            kind=Notification.Kind.INCIDENT_OPENED,
            title=display,
        )
    return incident, created


def resolve_incident(server, incident_type):
    incidents = Incident.objects.filter(
        server=server,
        type=incident_type,
        status=Incident.Status.OPEN,
    )
    for incident in incidents:
        if incident.resolve():
            display = incident.get_type_display()
            if incident.type == Incident.Type.DOWN:
                detail = f"{server.name} is running again and the {display.lower()} incident was closed."
            else:
                detail = f"{server.name} is back within limits and the {display.lower()} incident was closed."
            create_notification(
                server.owner,
                detail,
                level="success",
                server=server,
                kind=Notification.Kind.INCIDENT_RESOLVED,
                title=f"{display} resolved",
            )
    return incidents.count()


def prune_expired_metric_snapshots():
    cutoff = timezone.now() - timedelta(days=settings.MINICLOUD_METRIC_RETENTION_DAYS)
    deleted, _ = MetricSnapshot.objects.filter(timestamp__lt=cutoff).delete()
    return deleted


def _cpu_share(server, cpu_percent):
    """Docker reports cpu_percent against one host core; alerts need the container's own quota."""
    quota = Decimal(server.effective_cpu_limit)
    if quota <= 0:
        return Decimal(cpu_percent)
    # cpu_percent is already a percentage, so dividing by the core quota yields percent-of-quota.
    return Decimal(cpu_percent) / quota


def _memory_share(server, memory_mb):
    return (Decimal(memory_mb) / Decimal(server.effective_memory_mb)) * 100


def _is_sustained_breach(server, field_name, threshold, share_of):
    window_seconds = 60
    now = timezone.now()
    cutoff = now - timedelta(seconds=window_seconds + settings.MINICLOUD_METRIC_INTERVAL_SECONDS)
    samples = MetricSnapshot.objects.filter(server=server, timestamp__gte=cutoff).order_by("timestamp").values_list(
        "timestamp", field_name
    )
    return _has_sustained_breach(samples, lambda value: share_of(value) > threshold, now)


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
