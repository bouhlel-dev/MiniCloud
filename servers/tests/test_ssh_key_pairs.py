import base64
import re
import struct
from unittest.mock import MagicMock, Mock, patch

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat, load_ssh_private_key
from django.core.exceptions import ValidationError
from rest_framework.test import APIClient

from accounts.models import User
from servers import services
from servers.docker_service import DockerService, DockerServiceError
from servers.forms import ServerSSHKeyForm
from servers.models import Server, ServerSSHKey, ServerTemplate
from servers.ssh_keys import fingerprint_of, generate_key_pair

SSHD_MARKER = "PermitRootLogin=prohibit-password"


def ssh_public_key(algorithm="ssh-ed25519"):
    blob = struct.pack(">I", len(algorithm)) + algorithm.encode() + struct.pack(">I", 32) + bytes(range(32))
    return f"{algorithm} {base64.b64encode(blob).decode()} owner@laptop"


def running_container():
    container = MagicMock()
    container.id = "container-id"
    container.exec_run.return_value.exit_code = 0
    return container


def published_keys(container):
    """Decode the authorized_keys the sshd script actually writes."""
    scripts = [call.args[0][2] for call in container.exec_run.call_args_list if SSHD_MARKER in call.args[0][2]]
    payload = re.search(r"echo (\S*) \| base64", scripts[-1]).group(1)
    return base64.b64decode(payload).decode("utf-8")


@pytest.fixture
def api_client(settings):
    settings.ALLOWED_HOSTS = ["testserver"]
    return APIClient()


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="keys-owner", password="password")


@pytest.fixture
def other_owner(db):
    return User.objects.create_user(username="keys-visitor", password="password")


@pytest.fixture
def template(db):
    return ServerTemplate.objects.create(name="Keys", cpu_limit="0.50", memory_mb=256)


@pytest.fixture
def ssh_server(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="keys-web",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        container_id="container-id",
    )
    server.transition_to(Server.Status.RUNNING)
    ServerSSHKey.objects.create(server=server, label="laptop", public_key=ssh_public_key())
    return server


def test_generated_pair_is_a_usable_ed25519_key():
    pair = generate_key_pair("work laptop")

    private_key = load_ssh_private_key(pair["private_key"].encode(), password=None)
    public_line = private_key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode("ascii")
    assert pair["private_key"].startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert public_line.startswith("ssh-ed25519 ")
    assert pair["public_key"] == f"{public_line} work laptop"
    assert pair["fingerprint"] == fingerprint_of(pair["public_key"])


def test_a_label_with_stray_whitespace_still_becomes_one_comment():
    assert generate_key_pair("  kitchen   server  ")["public_key"].endswith(" kitchen server")


def test_a_mangled_public_key_has_no_fingerprint():
    assert fingerprint_of("ssh-ed25519 not-base64") == ""


def test_generating_a_key_returns_the_private_half_and_stores_only_the_public_half(ssh_server):
    key, private_key = services.create_server_ssh_key(ssh_server, label="phone", docker_service=Mock())

    assert "BEGIN OPENSSH PRIVATE KEY" in private_key
    stored = ServerSSHKey.objects.get(pk=key.pk)
    assert stored.public_key.startswith("ssh-ed25519 ")
    assert "PRIVATE KEY" not in stored.public_key


def test_pasting_a_key_returns_no_private_half(ssh_server):
    key, private_key = services.create_server_ssh_key(
        ssh_server, label="desk", public_key=f"  {ssh_public_key('ssh-rsa')}  ", docker_service=Mock()
    )

    assert private_key == ""
    assert key.public_key == ssh_public_key("ssh-rsa")


def test_adding_a_key_rewrites_authorized_keys_of_a_running_server(ssh_server):
    docker = Mock()

    services.create_server_ssh_key(ssh_server, label="phone", public_key=ssh_public_key("ssh-rsa"), docker_service=docker)

    docker.configure_ssh.assert_called_once_with(ssh_server)


def test_adding_a_key_to_a_stopped_server_waits_for_the_next_start(ssh_server):
    ssh_server.transition_to(Server.Status.STOPPED)
    docker = Mock()

    services.create_server_ssh_key(ssh_server, label="phone", docker_service=docker)

    docker.configure_ssh.assert_not_called()


def test_a_docker_outage_rolls_the_new_key_back(ssh_server):
    docker = Mock()
    docker.configure_ssh.side_effect = DockerServiceError("Docker is unavailable.")

    with pytest.raises(DockerServiceError):
        services.create_server_ssh_key(ssh_server, label="phone", docker_service=docker)

    assert not ssh_server.ssh_keys.filter(label="phone").exists()


def test_deleting_the_last_key_leaves_sshd_accepting_nobody(ssh_server):
    docker = Mock()

    services.delete_server_ssh_key(ssh_server, ssh_server.ssh_keys.get(), docker_service=docker)

    assert ssh_server.ssh_keys.count() == 0
    docker.configure_ssh.assert_called_once_with(ssh_server)


def test_every_key_becomes_its_own_line_with_its_own_sources(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="two-keys",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        ssh_allowed_from="203.0.113.7/32",
        container_id="container-id",
    )
    laptop = ServerSSHKey.objects.create(server=server, label="laptop", public_key=ssh_public_key())
    phone = ServerSSHKey.objects.create(server=server, label="phone", public_key=ssh_public_key("ssh-rsa"), allowed_from="198.51.100.9/32")
    container = running_container()
    client = MagicMock()
    client.containers.get.return_value = container

    DockerService(client=client).configure_ssh(server)

    assert published_keys(container) == (
        f'from="203.0.113.7/32" {laptop.public_key}\nfrom="198.51.100.9/32" {phone.public_key}\n'
    )


def test_a_key_with_no_sources_anywhere_is_published_bare(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="open-keys",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        container_id="container-id",
    )
    key = ServerSSHKey.objects.create(server=server, label="laptop", public_key=ssh_public_key())
    container = running_container()
    client = MagicMock()
    client.containers.get.return_value = container

    DockerService(client=client).configure_ssh(server)

    assert published_keys(container) == f"{key.public_key}\n"


def test_a_server_without_keys_publishes_an_empty_authorized_keys(owner, template):
    server = Server.objects.create(
        owner=owner,
        name="locked-out",
        image=Server.Image.NGINX,
        template=template,
        host_port=10100,
        ssh_enabled=True,
        ssh_port=10101,
        container_id="container-id",
    )
    container = running_container()
    client = MagicMock()
    client.containers.get.return_value = container

    DockerService(client=client).configure_ssh(server)

    assert published_keys(container) == ""


def test_one_label_per_server(owner, template, ssh_server):
    with pytest.raises(ValidationError, match="already"):
        ServerSSHKey(server=ssh_server, label="laptop", public_key=ssh_public_key("ssh-rsa")).full_clean()

    second = Server.objects.create(
        owner=owner, name="other-web", image=Server.Image.NGINX, template=template, host_port=10102
    )
    ServerSSHKey(server=second, label="laptop", public_key=ssh_public_key("ssh-rsa")).full_clean()


def test_an_empty_label_never_reaches_sshd(ssh_server):
    with pytest.raises(ValidationError, match="device"):
        ServerSSHKey(server=ssh_server, label="  ", public_key=ssh_public_key()).full_clean()


def test_a_duplicate_label_never_reaches_docker(ssh_server):
    docker = Mock()

    with pytest.raises(ValidationError):
        services.create_server_ssh_key(ssh_server, label="laptop", public_key=ssh_public_key("ssh-rsa"), docker_service=docker)

    docker.configure_ssh.assert_not_called()
    assert ssh_server.ssh_keys.count() == 1


@pytest.mark.parametrize("value", ["ssh-ed25519", "not-a-key AAAAB3NzaC1yc2E=", "ssh-rsa !!!not-base64!!! me"])
def test_the_key_form_rejects_anything_sshd_could_not_read(ssh_server, value):
    form = ServerSSHKeyForm(data={"label": "phone", "public_key": value})

    assert not form.is_valid()
    assert "public_key" in form.errors


def test_the_key_form_leaves_a_blank_public_key_as_a_request_to_generate(ssh_server):
    form = ServerSSHKeyForm(data={"label": "work  laptop", "public_key": "", "allowed_from": "203.0.113.7"})

    assert form.is_valid(), form.errors
    assert form.cleaned_data["label"] == "work laptop"
    assert form.cleaned_data["public_key"] == ""
    assert form.cleaned_data["allowed_from"] == "203.0.113.7/32"


def test_api_lists_the_public_halves_only(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)

    response = api_client.get(f"/api/servers/{ssh_server.pk}/ssh/keys/")

    assert response.status_code == 200
    assert [row["label"] for row in response.data] == ["laptop"]
    assert response.data[0]["fingerprint"].startswith("SHA256:")
    assert response.data[0]["sources"] == ""
    assert all("private_key" not in row for row in response.data)


def test_api_generation_hands_the_private_key_over_once(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)

    with patch("servers.services.DockerService", return_value=Mock()):
        created = api_client.post(f"/api/servers/{ssh_server.pk}/ssh/keys/", {"label": "phone"}, format="json")
        listed = api_client.get(f"/api/servers/{ssh_server.pk}/ssh/keys/")

    assert created.status_code == 201
    assert "BEGIN OPENSSH PRIVATE KEY" in created.data["private_key"]
    assert ServerSSHKey.objects.get(pk=created.data["id"]).public_key.startswith("ssh-ed25519 ")
    assert all("private_key" not in row for row in listed.data)


def test_api_reports_the_key_it_published_for_a_pasted_public_key(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)

    with patch("servers.services.DockerService", return_value=Mock()):
        created = api_client.post(
            f"/api/servers/{ssh_server.pk}/ssh/keys/",
            {"label": "desk", "public_key": ssh_public_key("ssh-rsa"), "allowed_from": "203.0.113.7"},
            format="json",
        )

    assert created.status_code == 201
    assert "private_key" not in created.data
    assert created.data["sources"] == "203.0.113.7/32"


def test_api_rejects_a_duplicate_label(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)

    response = api_client.post(
        f"/api/servers/{ssh_server.pk}/ssh/keys/",
        {"label": "laptop", "public_key": ssh_public_key("ssh-rsa")},
        format="json",
    )

    assert response.status_code == 400
    assert "already exists" in response.data["__all__"][0]


def test_api_rejects_a_hostname_as_a_per_key_allowlist(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)

    response = api_client.post(
        f"/api/servers/{ssh_server.pk}/ssh/keys/",
        {"label": "phone", "allowed_from": "office.example.com"},
        format="json",
    )

    assert response.status_code == 400
    assert "allowed_from" in response.data


def test_api_deletes_one_key_and_republishes_the_rest(api_client, owner, ssh_server):
    api_client.force_authenticate(user=owner)
    docker = Mock()
    key = ssh_server.ssh_keys.get()

    with patch("servers.services.DockerService", return_value=docker):
        response = api_client.delete(f"/api/servers/{ssh_server.pk}/ssh/keys/{key.pk}/")

    assert response.status_code == 204
    assert not ServerSSHKey.objects.filter(pk=key.pk).exists()
    docker.configure_ssh.assert_called_once_with(ssh_server)


def test_api_never_shows_another_users_keys(api_client, other_owner, ssh_server):
    api_client.force_authenticate(user=other_owner)

    assert api_client.get(f"/api/servers/{ssh_server.pk}/ssh/keys/").status_code == 404
    assert api_client.post(f"/api/servers/{ssh_server.pk}/ssh/keys/", {"label": "phone"}, format="json").status_code == 404
