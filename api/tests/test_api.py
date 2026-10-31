from unittest.mock import Mock, patch

import pytest
from rest_framework.test import APIClient

from accounts.models import User
from servers.models import Server, ServerTemplate
from servers.services import QuotaExceededError, provision_server


@pytest.fixture
def api_client(settings):
    settings.ALLOWED_HOSTS = ["testserver"]
    return APIClient()


@pytest.fixture
def server_template(db):
    return ServerTemplate.objects.create(name="Test", cpu_limit="0.50", memory_mb=256)


@pytest.fixture
def user(db):
    return User.objects.create_user(username="owner", password="password")


@pytest.fixture
def server(user, server_template):
    instance = Server.objects.create(
        owner=user,
        name="web",
        image=Server.Image.NGINX,
        template=server_template,
        container_id="container-id",
    )
    instance.transition_to(Server.Status.RUNNING)
    return instance


def test_api_requires_authentication(api_client):
    response = api_client.get("/api/servers/")
    assert response.status_code == 401


def test_token_endpoint_issues_token(api_client, user):
    response = api_client.post("/api/token/", {"username": "owner", "password": "password"}, format="json")
    assert response.status_code == 200
    assert response.data["token"]


def test_server_list_is_scoped_to_owner(api_client, user, server):
    other = User.objects.create_user(username="other", password="password")
    api_client.force_authenticate(user=other)
    response = api_client.get("/api/servers/")
    assert response.status_code == 200
    assert response.data == []


def test_start_action_delegates_to_service(api_client, user, server):
    api_client.force_authenticate(user=user)
    with patch("api.views.start_server", return_value=server) as start:
        response = api_client.post(f"/api/servers/{server.pk}/start/")
    assert response.status_code == 200
    start.assert_called_once()


def test_execute_action_returns_container_result(api_client, user, server):
    api_client.force_authenticate(user=user)
    with patch("api.views.execute_server_command", return_value={"exit_code": 0, "output": "5\n"}) as execute:
        response = api_client.post(
            f"/api/servers/{server.pk}/execute/",
            {"command": 'python -c "print(2 + 3)"'},
            format="json",
        )
    assert response.status_code == 200
    assert response.data == {"exit_code": 0, "output": "5\n"}
    execute.assert_called_once_with(server, 'python -c "print(2 + 3)"')


def test_create_delegates_to_provisioning_service(api_client, user, server_template):
    api_client.force_authenticate(user=user)
    created = Server.objects.create(owner=user, name="provisioned", image=Server.Image.NGINX, template=server_template)
    with patch("api.views.provision_server", return_value=created) as provision:
        response = api_client.post(
            "/api/servers/",
            {"name": "api-web", "image": "nginx", "template": server_template.pk, "env_vars": {}},
            format="json",
        )
    assert response.status_code == 201
    assert provision.call_args.args[0] == user
    assert provision.call_args.kwargs["template"] == server_template


def test_create_rejects_framework_for_another_runtime(api_client, user, server_template):
    api_client.force_authenticate(user=user)

    response = api_client.post(
        "/api/servers/",
        {"name": "invalid-framework", "image": "python", "framework": "express", "template": server_template.pk, "env_vars": {}},
        format="json",
    )

    assert response.status_code == 400
    assert "framework" in response.data


def test_create_rejects_database_limit_for_non_postgres(api_client, user, server_template):
    api_client.force_authenticate(user=user)

    response = api_client.post(
        "/api/servers/",
        {"name": "invalid-storage-limit", "image": "nginx", "database_size_mb": 512, "template": server_template.pk, "env_vars": {}},
        format="json",
    )

    assert response.status_code == 400
    assert "database_size_mb" in response.data


def test_metrics_returns_oldest_first(api_client, user, server):
    from monitoring.models import MetricSnapshot

    MetricSnapshot.objects.create(server=server, cpu_percent="10", memory_mb="20", net_rx=1, net_tx=2)
    MetricSnapshot.objects.create(server=server, cpu_percent="30", memory_mb="40", net_rx=3, net_tx=4)
    api_client.force_authenticate(user=user)
    response = api_client.get(f"/api/servers/{server.pk}/metrics/")
    assert response.status_code == 200
    assert [item["cpu_percent"] for item in response.data] == ["10.00", "30.00"]


def test_quota_rejects_provisioning(db, user, server_template):
    user.max_servers = 0
    user.save(update_fields=["max_servers"])
    docker = Mock()
    with pytest.raises(QuotaExceededError):
        provision_server(
            user,
            name="blocked",
            image=Server.Image.NGINX,
            custom_image="",
            template=server_template,
            env_vars={},
            docker_service=docker,
        )
    docker.create_container.assert_not_called()


def test_notifications_are_scoped_to_the_authenticated_user(api_client, user, db):
    from monitoring.models import Notification

    other = User.objects.create_user(username="other", password="password")
    Notification.objects.create(user=other, message="not yours")
    Notification.objects.create(user=user, message="incident opened", level="warning")

    api_client.force_authenticate(user=user)
    response = api_client.get("/api/notifications/")

    assert response.status_code == 200
    assert [item["message"] for item in response.data] == ["incident opened"]
    assert response.data[0]["level"] == "warning"
    assert response.data[0]["is_read"] is False


def test_mark_all_read_only_touches_the_authenticated_user(api_client, user, db):
    from monitoring.models import Notification

    other = User.objects.create_user(username="other", password="password")
    mine = Notification.objects.create(user=user, message="mine")
    theirs = Notification.objects.create(user=other, message="theirs")

    api_client.force_authenticate(user=user)
    response = api_client.post("/api/notifications/mark_all_read/")

    assert response.status_code == 200
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.is_read is True
    assert theirs.is_read is False


def test_notification_payload_carries_the_linked_server(api_client, user, server, db):
    from monitoring.models import Notification

    Notification.objects.create(
        user=user,
        server=server,
        kind=Notification.Kind.INCIDENT_OPENED,
        title="High CPU",
        message="web has been over the high cpu threshold.",
        level="warning",
    )

    api_client.force_authenticate(user=user)
    response = api_client.get("/api/notifications/")

    assert response.status_code == 200
    assert response.data[0]["kind"] == "incident_opened"
    assert response.data[0]["kind_display"] == "Incident opened"
    assert response.data[0]["title"] == "High CPU"
    assert response.data[0]["server"] == server.pk
    assert response.data[0]["server_name"] == "web"
    assert response.data[0]["server_status"] == server.status


def test_mark_read_only_touches_the_authenticated_user(api_client, user, db):
    from monitoring.models import Notification

    mine = Notification.objects.create(user=user, message="mine")
    theirs = Notification.objects.create(user=User.objects.create_user(username="other", password="password"), message="theirs")

    api_client.force_authenticate(user=user)
    response = api_client.post(f"/api/notifications/{theirs.pk}/mark_read/")
    assert response.status_code == 404

    response = api_client.post(f"/api/notifications/{mine.pk}/mark_read/")
    assert response.status_code == 200
    assert response.data["is_read"] is True
    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.is_read is True
    assert theirs.is_read is False


def test_health_check_recovers_a_failed_server_whose_container_is_back(db, user, server, settings):
    from monitoring.models import Incident, Notification
    from monitoring.services import open_incident
    from monitoring.tasks import check_server_health

    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

    server.transition_to(Server.Status.FAILED)
    open_incident(server, Incident.Type.DOWN)

    docker = Mock()
    docker.get_container_status.return_value = "running"
    with patch("monitoring.tasks.DockerService", return_value=docker):
        result = check_server_health()

    server.refresh_from_db()
    incident = Incident.objects.get(server=server, type=Incident.Type.DOWN)
    assert result == {"healthy": 1, "unhealthy": 0}
    assert server.status == Server.Status.RUNNING
    assert incident.status == Incident.Status.RESOLVED
    resolution = Notification.objects.get(user=user, kind=Notification.Kind.INCIDENT_RESOLVED)
    assert resolution.level == "success"
    assert resolution.server == server


def test_health_check_inspects_each_server_once(db, user, server, settings):
    from monitoring.models import Incident
    from monitoring.tasks import check_server_health

    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    Incident.objects.create(server=server, type=Incident.Type.HIGH_CPU)
    Incident.objects.create(server=server, type=Incident.Type.HIGH_MEMORY, status=Incident.Status.RESOLVED)

    docker = Mock()
    docker.get_container_status.return_value = "running"
    with patch("monitoring.tasks.DockerService", return_value=docker):
        result = check_server_health()

    assert result == {"healthy": 1, "unhealthy": 0}
    docker.get_container_status.assert_called_once_with("container-id")


def test_health_check_ignores_a_deliberately_stopped_server(db, user, server, settings):
    from monitoring.tasks import check_server_health

    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    server.transition_to(Server.Status.STOPPED)

    docker = Mock()
    with patch("monitoring.tasks.DockerService", return_value=docker):
        result = check_server_health()

    assert result == {"healthy": 0, "unhealthy": 0}
    docker.get_container_status.assert_not_called()


def test_models_have_no_unmigrated_changes(db):
    """Catches a model change that was never turned into a migration file."""
    from django.apps import apps
    from django.db.migrations.autodetector import MigrationAutodetector
    from django.db.migrations.loader import MigrationLoader
    from django.db.migrations.state import ProjectState

    loader = MigrationLoader(None, ignore_no_migrations=True)
    changes = MigrationAutodetector(
        loader.project_state(),
        ProjectState.from_apps(apps),
    ).changes(graph=loader.graph)
    changed_apps = set(changes)

    assert not changed_apps, f"Models changed without a migration: {sorted(changed_apps)}"
