from celery import shared_task

from servers.docker_service import DockerContainerNotFound, DockerService, DockerServiceError
from servers.models import Server

from .services import (
    handle_unhealthy_server,
    prune_expired_metric_snapshots,
    record_healthy_check,
    record_metric_snapshot,
)


@shared_task
def collect_server_metrics():
    docker_service = DockerService()
    collected = 0
    failures = 0
    servers = Server.objects.filter(status=Server.Status.RUNNING).select_related("template")
    for server in servers.iterator():
        try:
            stats = docker_service.get_container_stats(server.container_id)
            record_metric_snapshot(server, stats)
        except DockerServiceError:
            failures += 1
        else:
            collected += 1
    return {"collected": collected, "failures": failures}


@shared_task
def check_server_health():
    docker_service = DockerService()
    healthy = 0
    unhealthy = 0
    servers = Server.objects.filter(status=Server.Status.RUNNING).select_related("owner")
    for server in servers.iterator():
        try:
            container_status = docker_service.get_container_status(server.container_id)
        except DockerContainerNotFound as error:
            handle_unhealthy_server(server, "missing", docker_service, error)
            unhealthy += 1
            continue
        except DockerServiceError as error:
            handle_unhealthy_server(server, "unknown", docker_service, error)
            unhealthy += 1
            continue

        if container_status == "running":
            record_healthy_check(server, container_status)
            healthy += 1
        else:
            handle_unhealthy_server(server, container_status, docker_service)
            unhealthy += 1
    return {"healthy": healthy, "unhealthy": unhealthy}


@shared_task
def prune_metric_snapshots():
    return {"deleted": prune_expired_metric_snapshots()}
