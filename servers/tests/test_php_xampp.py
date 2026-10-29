from unittest.mock import MagicMock

import pytest

from accounts.models import User
from api.serializers import ServerSerializer
from servers.docker_service import DockerService, DockerServiceError
from servers.models import Server, ServerTemplate


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="php-owner", password="password")


@pytest.fixture
def template(db):
    return ServerTemplate.objects.create(name="PHP", cpu_limit="1.00", memory_mb=512)


def test_php_server_runs_xampp_and_exposes_its_web_port(owner, template, settings):
    settings.MINICLOUD_XAMPP_INSTALLER_URL = "https://downloads.example.test/xampp-installer.run"
    server = Server(
        owner=owner,
        name="php-site",
        image=Server.Image.PHP,
        template=template,
        host_port=18080,
    )
    container = MagicMock(id="container-id")
    container.status = "running"
    container.logs.return_value = b"MINICLOUD_XAMPP_READY: XAMPP is serving on port 80"
    client = MagicMock()
    client.containers.run.return_value = container

    container_id = DockerService(client=client).create_container(server)

    command = client.containers.run.call_args.kwargs["command"]
    assert client.containers.run.call_args.args[0] == "debian:bookworm-slim"
    assert client.containers.run.call_args.kwargs["ports"] == {"80/tcp": 18080}
    # XAMPP needs more than the 512 MB template; the floor must be applied.
    assert client.containers.run.call_args.kwargs["mem_limit"] == 1024 * 1024 * 1024
    assert command[:2] == ["sh", "-c"]
    assert "https://downloads.example.test/xampp-installer.run" in command[2]
    assert "/opt/lampp/lampp start" in command[2]
    assert ".minicloud-installed" in command[2]
    assert "nano" in command[2]
    assert container_id == "container-id"
    container.logs.assert_called()


def test_php_readiness_reports_out_of_memory(owner, template):
    server = Server(
        owner=owner,
        name="php-oom",
        image=Server.Image.PHP,
        template=template,
        host_port=18081,
    )
    container = MagicMock(id="container-id")
    container.status = "exited"
    container.logs.return_value = b""
    container.attrs = {"State": {"OOMKilled": True, "ExitCode": 137}}

    with pytest.raises(DockerServiceError, match="ran out of memory \\(1024 MB\\)"):
        DockerService(client=MagicMock())._wait_for_xampp_ready(container, server=server, max_wait=1, interval=0.01)


def test_php_server_includes_a_published_url(owner, template):
    server = Server(
        owner=owner,
        name="php-site-url",
        image=Server.Image.PHP,
        template=template,
        host_port=18080,
    )

    assert ServerSerializer(server).data["site_url"] == "http://127.0.0.1:18080"
