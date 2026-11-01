import base64
import socket
import struct
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from servers import services
from servers.docker_service import DockerService, DockerServiceError
from servers.forms import ServerProvisionForm
from servers.models import Server, ServerTemplate

SSHD_MARKER = "PermitRootLogin=prohibit-password"


def ssh_public_key(algorithm="ssh-ed25519"):
    blob = struct.pack(">I", len(algorithm)) + algorithm.encode() + struct.pack(">I", 32) + bytes(range(32))
    return f"{algorithm} {base64.b64encode(blob).decode()} owner@laptop"


def executed_scripts(container):
    return [call.args[0][2] for call in container.exec_run.call_args_list]


def sshd_scripts(container):
    return [script for script in executed_scripts(container) if SSHD_MARKER in script]


def running_container():
    container = MagicMock()
    container.id = "container-id"
    container.exec_run.return_value.exit_code = 0
    return container


@pytest.fixture
def api_client(settings):
    settings.ALLOWED_HOSTS = ["testserver"]
    return APIClient()


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="ssh-owner", password="password")


@pytest.fixture
def template(db):
    return ServerTemplate.objects.create(name="SSH", cpu_limit="0.50", memory_mb=256)


@pytest.fixture
def ssh_server(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="ssh-web",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        ssh_public_key=ssh_public_key(),
        container_id="container-id",
    )
    server.transition_to(Server.Status.RUNNING)
    return server


@pytest.mark.parametrize(
    "value",
    [
        "",
        "ssh-ed25519",
        "not-a-key AAAAB3NzaC1yc2E=",
        "ssh-rsa !!!not-base64!!! owner@laptop",
    ],
)
def test_ssh_keys_are_validated_before_they_reach_a_container(owner, template, value):
    server = Server(
        owner=owner,
        name="invalid-key",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_port=10101,
        ssh_enabled=True,
        ssh_public_key=value,
    )

    with pytest.raises(ValidationError):
        server.full_clean()


def test_disabled_ssh_needs_neither_key_nor_port(owner, template):
    server = Server(
        owner=owner,
        name="plain-web",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
    )
    server.full_clean()

    assert server.ssh_command is None


def test_enabling_ssh_on_a_saved_server_requires_a_port(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="no-ssh-port",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
    )
    server.ssh_enabled = True
    server.ssh_public_key = ssh_public_key()

    with pytest.raises(ValidationError, match="SSH host port"):
        server.full_clean()


def test_a_new_server_may_not_have_an_ssh_port_yet(owner, template):
    # The provisioning service allocates the port after the forms have validated.
    server = Server(
        owner=owner,
        name="about-to-provision",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_public_key=ssh_public_key(),
    )

    server.full_clean()


def test_ssh_port_cannot_reuse_the_application_port(owner, template):
    server = Server(
        owner=owner,
        name="same-port",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10100,
        ssh_public_key=ssh_public_key(),
    )

    with pytest.raises(ValidationError, match="must differ"):
        server.full_clean()


def test_ssh_command_advertises_the_configured_host(ssh_server, settings):
    settings.MINICLOUD_SSH_HOST = "ssh.example.net"

    assert ssh_server.ssh_command == "ssh -p 10101 root@ssh.example.net"


def test_enabled_ssh_publishes_port_22_and_installs_sshd(owner, template):
    server = Server(
        owner=owner,
        name="ssh-nginx",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        ssh_public_key=ssh_public_key(),
    )
    container = running_container()
    client = MagicMock()
    client.containers.run.return_value = container

    DockerService(client=client).create_container(server)

    assert client.containers.run.call_args.kwargs["ports"] == {"80/tcp": 10100, "22/tcp": 10101}
    assert len(sshd_scripts(container)) == 1
    key_blob = base64.b64encode(f"{server.ssh_public_key}\n".encode()).decode("ascii")
    assert key_blob in sshd_scripts(container)[0]


def test_disabled_ssh_leaves_port_22_closed(owner, template):
    server = Server(
        owner=owner,
        name="quiet-nginx",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
    )
    container = running_container()
    client = MagicMock()
    client.containers.run.return_value = container

    DockerService(client=client).create_container(server)

    assert client.containers.run.call_args.kwargs["ports"] == {"80/tcp": 10100}
    assert sshd_scripts(container) == []


def test_a_broken_sshd_rolls_the_container_back(owner, template):
    server = Server(
        owner=owner,
        name="failing-ssh",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        ssh_public_key=ssh_public_key(),
    )
    container = running_container()

    def exec_run(command, **kwargs):
        result = MagicMock()
        result.exit_code = 1 if SSHD_MARKER in command[2] else 0
        return result

    container.exec_run.side_effect = exec_run
    client = MagicMock()
    client.containers.run.return_value = container

    with pytest.raises(DockerServiceError, match="SSH"):
        DockerService(client=client).create_container(server)

    container.remove.assert_called_once_with(force=True)


def test_starting_a_server_brings_sshd_back_up(ssh_server):
    container = running_container()
    client = MagicMock()
    client.containers.get.return_value = container

    DockerService(client=client).start_container("container-id", server=ssh_server)

    assert len(sshd_scripts(container)) == 1

    container.exec_run.reset_mock()
    DockerService(client=client).restart_container("container-id")

    assert sshd_scripts(container) == []


def test_port_allocation_skips_reserved_and_published_ssh_ports(ssh_server, settings):
    settings.MINICLOUD_PORT_RANGE_START = 10100
    settings.MINICLOUD_PORT_RANGE_END = 10105
    service = DockerService(client=MagicMock())
    service._is_locally_available = lambda port: True

    # 10100 is the app port of the seeded server, 10101 its ssh port, and 10102
    # is reserved by the caller for the port it has not published yet.
    assert service.find_available_host_port(reserved={10102}) == 10103


def test_provisioning_reserves_a_second_port_for_ssh(owner, template):
    docker = Mock()
    docker.find_available_host_port.side_effect = [10100, 10101]

    with patch("servers.services._spawn_provisioning"):
        server = services.provision_server(
            owner,
            name="ssh-web",
            image=Server.Image.NGINX,
            custom_image="",
            template=template,
            env_vars={},
            ssh_enabled=True,
            ssh_public_key=f"  {ssh_public_key()}  ",
            docker_service=docker,
        )

    assert (server.host_port, server.ssh_port) == (10100, 10101)
    assert server.ssh_public_key == ssh_public_key()
    docker.find_available_host_port.assert_any_call(reserved={10100})


def test_provisioning_without_ssh_keeps_one_port(owner, template):
    docker = Mock()
    docker.find_available_host_port.return_value = 10100

    with patch("servers.services._spawn_provisioning"):
        server = services.provision_server(
            owner,
            name="plain-web",
            image=Server.Image.NGINX,
            custom_image="",
            template=template,
            env_vars={},
            ssh_enabled=False,
            ssh_public_key=ssh_public_key(),
            docker_service=docker,
        )

    assert server.ssh_port is None
    assert server.ssh_public_key == ""
    docker.find_available_host_port.assert_called_once_with()


def test_deleting_a_server_frees_the_ssh_port(ssh_server):
    services.delete_server(ssh_server, docker_service=Mock())

    ssh_server.refresh_from_db()
    assert (ssh_server.host_port, ssh_server.ssh_port) == (None, None)


def test_api_passes_ssh_options_to_provisioning(api_client, owner, template):
    api_client.force_authenticate(user=owner)
    created = Server.objects.create(owner=owner, name="created", image=Server.Image.NGINX, template=template)

    with patch("api.views.provision_server", return_value=created) as provision:
        response = api_client.post(
            "/api/servers/",
            {
                "name": "api-ssh",
                "image": "nginx",
                "template": template.pk,
                "env_vars": {},
                "ssh_enabled": True,
                "ssh_public_key": ssh_public_key(),
            },
            format="json",
        )

    assert response.status_code == 201
    assert provision.call_args.kwargs["ssh_enabled"] is True
    assert provision.call_args.kwargs["ssh_public_key"] == ssh_public_key()


def test_api_rejects_ssh_without_a_key(api_client, owner, template):
    api_client.force_authenticate(user=owner)

    response = api_client.post(
        "/api/servers/",
        {
            "name": "api-no-key",
            "image": "nginx",
            "template": template.pk,
            "env_vars": {},
            "ssh_enabled": True,
        },
        format="json",
    )

    assert response.status_code == 400
    assert "ssh_public_key" in response.data


def test_replacing_the_key_reconfigures_a_running_container(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)
    replacement = ssh_public_key("ssh-rsa")
    docker = Mock()

    with patch("api.views.DockerService", return_value=docker):
        response = api_client.patch(f"/api/servers/{ssh_server.pk}/", {"ssh_public_key": replacement}, format="json")

    assert response.status_code == 200
    ssh_server.refresh_from_db()
    assert ssh_server.ssh_public_key == replacement
    docker.configure_ssh.assert_called_once_with(ssh_server)
    assert response.data["ssh_command"].startswith("ssh -p 10101 root@")


def test_api_never_lets_a_client_assign_the_ssh_port(api_client, owner, template):
    api_client.force_authenticate(user=owner)
    created = Server.objects.create(owner=owner, name="created", image=Server.Image.NGINX, template=template)

    with patch("api.views.provision_server", return_value=created) as provision:
        api_client.post(
            "/api/servers/",
            {
                "name": "api-port",
                "image": "nginx",
                "template": template.pk,
                "env_vars": {},
                "ssh_enabled": True,
                "ssh_public_key": ssh_public_key(),
                "ssh_port": 22,
            },
            format="json",
        )

    assert "ssh_port" not in provision.call_args.kwargs


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("203.0.113.7", "203.0.113.7/32"),
        (" 203.0.113.7 , 198.51.100.0/24,203.0.113.7 ", "203.0.113.7/32,198.51.100.0/24"),
        ("198.51.100.0/24\n203.0.113.7", "198.51.100.0/24,203.0.113.7/32"),
    ],
)
def test_allowed_from_is_normalised_to_cidr(value, expected):
    assert Server.parse_ssh_allowed_from(value) == expected


@pytest.mark.parametrize("value", ["office.example.com", "203.0.113.7; reboot", "300.1.1.1", "10.0.0.0/33"])
def test_allowed_from_rejects_hostnames_and_impossible_addresses(value):
    with pytest.raises(ValidationError):
        Server.parse_ssh_allowed_from(value)


def test_a_bad_allowlist_is_rejected_by_the_model(owner, template):
    server = Server(
        owner=owner,
        name="bad-allowlist",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        ssh_public_key=ssh_public_key(),
        ssh_allowed_from="office.example.com",
    )

    with pytest.raises(ValidationError, match="not an IP address"):
        server.full_clean()


def test_the_allowlist_becomes_a_from_option_on_the_key(owner, template):
    server = Server(
        owner=owner,
        name="locked-ssh",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        ssh_public_key=ssh_public_key(),
        ssh_allowed_from="203.0.113.7",
    )
    container = running_container()
    client = MagicMock()
    client.containers.run.return_value = container

    DockerService(client=client).create_container(server)

    key_line = f'from="203.0.113.7/32" {server.ssh_public_key}'
    assert base64.b64encode(f"{key_line}\n".encode()).decode("ascii") in sshd_scripts(container)[0]


def test_sshd_starts_with_bruteforce_limits(ssh_server):
    container = running_container()
    client = MagicMock()
    client.containers.get.return_value = container

    DockerService(client=client).configure_ssh(ssh_server)

    script = sshd_scripts(container)[0]
    for option in (
        "PasswordAuthentication=no",
        "PermitEmptyPasswords=no",
        "KbdInteractiveAuthentication=no",
        "MaxAuthTries=3",
        "LoginGraceTime=30",
        "MaxStartups=10:30:60",
    ):
        assert option in script


@pytest.mark.parametrize(
    ("host", "exposed"),
    [
        ("localhost", False),
        ("web.local", False),
        ("10.5.0.7", False),
        ("192.168.1.20", False),
        ("100.64.0.5", False),
        ("ssh.example.net", True),
        ("203.0.113.7", True),
    ],
)
def test_exposure_of_the_advertised_ssh_host(ssh_server, settings, host, exposed):
    settings.MINICLOUD_SSH_HOST = host

    assert ssh_server.ssh_exposed is exposed


def stub_connection(banner):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.return_value = banner
    return connection


def test_reachability_reads_the_ssh_banner(ssh_server, settings):
    settings.MINICLOUD_SSH_HOST = "ssh.example.net"
    connection = stub_connection(b"SSH-2.0-OpenSSH_9.6\r\n")

    with patch("servers.services.socket.create_connection", return_value=connection) as connect:
        result = services.check_ssh_reachability(ssh_server)

    assert (result["state"], result["reachable"]) == ("open", True)
    assert connect.call_args.args[0] == ("ssh.example.net", 10101)
    assert "OpenSSH_9.6" in result["message"]


def test_reachability_notices_a_forward_to_the_wrong_port(ssh_server):
    connection = stub_connection(b"HTTP/1.1 400 Bad Request\r\n")

    with patch("servers.services.socket.create_connection", return_value=connection):
        result = services.check_ssh_reachability(ssh_server)

    assert result["state"] == "not_ssh"
    assert result["reachable"] is False
    assert "not sshd" in result["message"]


def test_a_timeout_on_an_internet_host_suggests_testing_from_outside(ssh_server, settings):
    settings.MINICLOUD_SSH_HOST = "ssh.example.net"

    with patch("servers.services.socket.create_connection", side_effect=TimeoutError):
        result = services.check_ssh_reachability(ssh_server)

    assert result["state"] == "timeout"
    assert "outside your network" in result["message"]


def test_a_refused_private_host_gets_no_loopback_note(ssh_server, settings):
    settings.MINICLOUD_SSH_HOST = "10.5.0.7"

    with patch("servers.services.socket.create_connection", side_effect=ConnectionRefusedError):
        result = services.check_ssh_reachability(ssh_server)

    assert result["state"] == "refused"
    assert "outside your network" not in result["message"]


def test_an_unresolved_ddns_name_is_named(ssh_server, settings):
    settings.MINICLOUD_SSH_HOST = "home.example.net"

    with patch("servers.services.socket.create_connection", side_effect=socket.gaierror("no such host")):
        result = services.check_ssh_reachability(ssh_server)

    assert result["state"] == "unresolved"
    assert "home.example.net" in result["message"]


def test_a_stopped_server_is_not_probed(ssh_server):
    ssh_server.transition_to(Server.Status.STOPPED)

    with patch("servers.services.socket.create_connection") as connect:
        result = services.check_ssh_reachability(ssh_server)

    assert result["state"] == "stopped"
    connect.assert_not_called()


def test_a_server_without_ssh_is_not_probed(owner, template):
    server = Server.objects.create(owner=owner, name="plain-web", image=Server.Image.NGINX, template=template, host_port=10100)

    with patch("servers.services.socket.create_connection") as connect:
        result = services.check_ssh_reachability(server)

    assert result["state"] == "disabled"
    connect.assert_not_called()


def test_ssh_check_endpoint_returns_the_probe(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)
    probe = {"host": "ssh.example.net", "port": 10101, "exposed": True, "allowed_from": "", "reachable": True, "state": "open", "message": "sshd answered"}

    with patch("api.views.check_ssh_reachability", return_value=probe) as check:
        response = api_client.post(f"/api/servers/{ssh_server.pk}/ssh/check/")

    assert response.status_code == 200
    assert response.data == probe
    assert check.call_args.args[0] == ssh_server


def test_replacing_the_allowlist_reconfigures_a_running_container(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)
    docker = Mock()

    with patch("api.views.DockerService", return_value=docker):
        response = api_client.patch(f"/api/servers/{ssh_server.pk}/", {"ssh_allowed_from": "203.0.113.7"}, format="json")

    assert response.status_code == 200
    ssh_server.refresh_from_db()
    assert ssh_server.ssh_allowed_from == "203.0.113.7/32"
    assert response.data["ssh_allowed_from"] == "203.0.113.7/32"
    docker.configure_ssh.assert_called_once_with(ssh_server)


def test_api_rejects_a_hostname_allowlist(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)

    response = api_client.patch(f"/api/servers/{ssh_server.pk}/", {"ssh_allowed_from": "office.example.com"}, format="json")

    assert response.status_code == 400
    assert "ssh_allowed_from" in response.data


def test_provisioning_stores_a_normalised_allowlist(owner, template):
    docker = Mock()
    docker.find_available_host_port.side_effect = [10100, 10101]

    with patch("servers.services._spawn_provisioning"):
        server = services.provision_server(
            owner,
            name="locked-web",
            image=Server.Image.NGINX,
            custom_image="",
            template=template,
            env_vars={},
            ssh_enabled=True,
            ssh_public_key=ssh_public_key(),
            ssh_allowed_from="203.0.113.7, 198.51.100.0/24",
            docker_service=docker,
        )

    assert server.ssh_allowed_from == "203.0.113.7/32,198.51.100.0/24"


def test_provisioning_without_ssh_drops_the_allowlist(owner, template):
    docker = Mock()
    docker.find_available_host_port.return_value = 10100

    with patch("servers.services._spawn_provisioning"):
        server = services.provision_server(
            owner,
            name="plain-web",
            image=Server.Image.NGINX,
            custom_image="",
            template=template,
            env_vars={},
            ssh_enabled=False,
            ssh_allowed_from="203.0.113.7",
            docker_service=docker,
        )

    assert server.ssh_allowed_from == ""


def test_provision_form_normalises_the_allowlist(owner, template):
    form = ServerProvisionForm(
        data={
            "name": "ssh-web",
            "image": Server.Image.NGINX,
            "custom_image": "",
            "template": template.pk,
            "env_vars": "{}",
            "ssh_enabled": "on",
            "ssh_public_key": ssh_public_key(),
            "ssh_allowed_from": "203.0.113.7",
        }
    )

    assert form.is_valid(), form.errors
    assert form.provisioning_data()["ssh_allowed_from"] == "203.0.113.7/32"


def test_provision_form_rejects_a_hostname_allowlist(owner, template):
    form = ServerProvisionForm(
        data={
            "name": "ssh-web",
            "image": Server.Image.NGINX,
            "custom_image": "",
            "template": template.pk,
            "env_vars": "{}",
            "ssh_enabled": "on",
            "ssh_public_key": ssh_public_key(),
            "ssh_allowed_from": "office.example.com",
        }
    )

    assert not form.is_valid()
    assert "ssh_allowed_from" in form.errors
