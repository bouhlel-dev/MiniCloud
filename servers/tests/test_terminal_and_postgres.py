from unittest.mock import MagicMock, call

import pytest

from accounts.models import User
from servers.docker_service import DockerService
from servers.forms import ServerProvisionForm
from servers.models import Server, ServerTemplate
from servers.services import open_server_terminal, postgres_connection_string


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="terminal-owner", password="password")


@pytest.fixture
def template(db):
    return ServerTemplate.objects.create(name="Terminal", cpu_limit="0.50", memory_mb=256)


def test_open_terminal_uses_interactive_tty():
    container = MagicMock()
    container.id = "container-id"
    container.status = "running"
    socket_handle = MagicMock()
    socket_handle.recv.return_value = b"shell output"
    client = MagicMock()
    client.containers.get.return_value = container
    client.api.exec_create.return_value = {"Id": "exec-id"}
    client.api.exec_start.return_value = socket_handle

    terminal = DockerService(client=client).open_terminal("container-id")

    client.api.exec_create.assert_called_once_with(
        "container-id",
        cmd=["/bin/sh", "-l"],
        stdin=True,
        stdout=True,
        stderr=True,
        tty=True,
    )
    client.api.exec_start.assert_called_once_with("exec-id", tty=True, socket=True)
    assert terminal.read() == b"shell output"
    terminal.enable_path_reporting()
    terminal.write(b"touch index.html\n")
    path_reporting = (
        b"stty -echo 2>/dev/null\n"
        b"PS1=$(printf '%s%s%s' '__MINICLOUD_' 'CWD__$(pwd)__MINICLOUD_' 'CWD__$ ')\n"
        b"stty echo 2>/dev/null\n"
        b"printf '\\033[2J\\033[H'\n"
    )
    socket_handle.sendall.assert_has_calls(
        [
            call(path_reporting),
            call(b"touch index.html\n"),
        ]
    )
    terminal.resize(120, 40)
    client.api.exec_resize.assert_called_once_with("exec-id", height=40, width=120)


def test_postgres_form_generates_required_environment(owner, template):
    form = ServerProvisionForm(
        data={
            "name": "database",
            "image": Server.Image.POSTGRES,
            "custom_image": "",
            "template": template.pk,
            "env_vars": "{}",
            "postgres_database": "app_db",
            "postgres_username": "app_user",
            "postgres_password": "",
        }
    )

    assert form.is_valid(), form.errors
    environment = form.provisioning_data()["env_vars"]
    assert environment["POSTGRES_DB"] == "app_db"
    assert environment["POSTGRES_USER"] == "app_user"
    assert len(environment["POSTGRES_PASSWORD"]) >= 12


def test_postgres_connection_string_is_escaped(owner, template, settings):
    settings.MINICLOUD_PUBLIC_HOST = "db.example.test"
    server = Server.objects.create(
        owner=owner,
        name="database",
        image=Server.Image.POSTGRES,
        template=template,
        host_port=15432,
        env_vars={
            "POSTGRES_DB": "app db",
            "POSTGRES_USER": "app@user",
            "POSTGRES_PASSWORD": "safe password/@",
        },
    )

    assert postgres_connection_string(server) == "postgresql://app%40user:safe%20password%2F%40@db.example.test:15432/app%20db"


def test_postgres_terminal_opens_connected_psql_session(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="database-terminal",
        image=Server.Image.POSTGRES,
        template=template,
        status=Server.Status.CREATING,
        env_vars={
            "POSTGRES_DB": "app_db",
            "POSTGRES_USER": "app_user",
            "POSTGRES_PASSWORD": "safe-password",
        },
    )
    server.transition_to(Server.Status.RUNNING)
    server.container_id = "container-id"
    server.save(update_fields=["container_id"])

    container = MagicMock(id="container-id", status="running")
    socket_handle = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    client.api.exec_create.return_value = {"Id": "exec-id"}
    client.api.exec_start.return_value = socket_handle

    terminal = open_server_terminal(server, docker_service=DockerService(client=client))

    client.api.exec_create.assert_called_once_with(
        "container-id",
        cmd=["psql", "-h", "127.0.0.1", "-U", "app_user", "-d", "app_db"],
        environment={"PGPASSWORD": "safe-password"},
        stdin=True,
        stdout=True,
        stderr=True,
        tty=True,
    )
    assert not terminal.supports_path_reporting


def test_deleted_server_name_can_be_reused(owner, template):
    deleted_server = Server.objects.create(
        owner=owner,
        name="reusable-name",
        image=Server.Image.NGINX,
        template=template,
    )
    deleted_server.mark_deleted()

    replacement = Server(
        owner=owner,
        name="reusable-name",
        image=Server.Image.NGINX,
        template=template,
    )
    replacement.full_clean()
    replacement.save()

    assert replacement.pk is not None
