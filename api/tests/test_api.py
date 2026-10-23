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
