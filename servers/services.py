import socket
import threading
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction

from accounts.models import User

from .docker_service import DockerService, DockerServiceError
from .models import Server, ServerSSHKey, ServerTemplate
from .ssh_keys import generate_key_pair


class ServerServiceError(Exception):
    pass


class QuotaExceededError(ServerServiceError):
    pass


class ProvisioningError(ServerServiceError):
    pass


class InvalidServerAction(ServerServiceError):
    pass


def postgres_connection_string(server):
    if server.image != Server.Image.POSTGRES or not server.host_port:
        return None
    environment = server.env_vars
    database = environment.get("POSTGRES_DB", "postgres")
    username = environment.get("POSTGRES_USER", "postgres")
    password = environment.get("POSTGRES_PASSWORD")
    if not password:
        return None
    return (
        "postgresql://"
        f"{quote(str(username), safe='')}:{quote(str(password), safe='')}"
        f"@{settings.MINICLOUD_PUBLIC_HOST}:{server.host_port}/{quote(str(database), safe='')}"
    )


def execute_server_command(server, command, docker_service=None):
    docker_service = docker_service or DockerService()
    if server.status != Server.Status.RUNNING:
        raise InvalidServerAction("Commands can only run on running servers.")
    try:
        return docker_service.execute_container_command(server.container_id, command)
    except DockerServiceError as error:
        raise ServerServiceError(str(error)) from error


def open_server_terminal(server, docker_service=None):
    docker_service = docker_service or DockerService()
    if server.status != Server.Status.RUNNING:
        raise InvalidServerAction("Interactive sessions require a running server.")

    if server.image != Server.Image.POSTGRES:
        try:
            return docker_service.open_terminal(server.container_id)
        except DockerServiceError as error:
            raise ServerServiceError(str(error)) from error

    environment = server.env_vars
    database = str(environment.get("POSTGRES_DB", "postgres"))
    username = str(environment.get("POSTGRES_USER", "postgres"))
    password = environment.get("POSTGRES_PASSWORD")
    if not password:
        raise ServerServiceError("PostgreSQL credentials are missing for this server.")

    try:
        return docker_service.open_terminal(
            server.container_id,
            command=["psql", "-h", "127.0.0.1", "-U", username, "-d", database],
            environment={"PGPASSWORD": str(password)},
            supports_path_reporting=False,
        )
    except DockerServiceError as error:
        raise ServerServiceError(str(error)) from error


def provision_server(owner, *, name, image, template, custom_image="", env_vars=None, framework="", database_size_mb=None, ssh_enabled=False, ssh_public_key="", ssh_allowed_from="", docker_service=None):
    port_service = docker_service or DockerService()
    env_vars = env_vars or {}
    if not ssh_enabled:
        ssh_public_key = ""
        ssh_allowed_from = ""
    else:
        # A key typed in at provisioning becomes the first of possibly many; leaving the
        # field blank is valid too, and the server page can generate a pair afterwards.
        try:
            ssh_public_key = Server.parse_ssh_public_key(ssh_public_key) if ssh_public_key.strip() else ""
            ssh_allowed_from = Server.parse_ssh_allowed_from(ssh_allowed_from)
        except ValidationError as error:
            raise ProvisioningError("; ".join(error.messages)) from error
    with transaction.atomic():
        locked_owner = User.objects.select_for_update().get(pk=owner.pk)
        locked_template = ServerTemplate.objects.get(pk=template.pk)
        if not locked_owner.has_capacity_for(locked_template, image=image):
            raise QuotaExceededError("Your server, CPU, or memory quota has been reached.")

        host_port = port_service.find_available_host_port()
        ssh_port = None
        if ssh_enabled:
            # The application port is not published yet, so it is still free on
            # the host; reserve it to avoid handing the same port to sshd.
            ssh_port = port_service.find_available_host_port(reserved={host_port})

        server = Server(
            owner=locked_owner,
            name=name,
            image=image,
            framework=framework,
            database_size_mb=database_size_mb,
            custom_image=custom_image,
            template=locked_template,
            host_port=host_port,
            ssh_enabled=ssh_enabled,
            ssh_port=ssh_port,
            ssh_allowed_from=ssh_allowed_from,
            env_vars=env_vars,
            provisioning_step=Server.ProvisioningStep.ALLOCATING_PORT,
        )
        try:
            server.full_clean()
        except ValidationError as error:
            raise ProvisioningError("; ".join(error.messages)) from error
        server.save()
        if ssh_public_key:
            server.ssh_keys.create(label="default", public_key=ssh_public_key)

    _spawn_provisioning(server.pk, docker_service)
    return server


def _spawn_provisioning(server_pk, docker_service=None):
    worker = threading.Thread(
        target=_run_provisioning,
        args=(server_pk, docker_service),
        daemon=True,
        name=f"minicloud-provision-{server_pk}",
    )
    worker.start()


def _run_provisioning(server_pk, docker_service=None):
    close_old_connections()
    server = Server.objects.filter(pk=server_pk).first()
    if not server:
        return
    try:
        service = docker_service or DockerService()
        container_id = service.create_container(server)
        server.container_id = container_id
        server.provisioning_step = Server.ProvisioningStep.READY
        server.provisioning_error = ""
        server.save(update_fields=["container_id", "provisioning_step", "provisioning_error"])
        server.transition_to(Server.Status.RUNNING)
    except Exception as error:
        server = Server.objects.filter(pk=server_pk).first()
        if server:
            server.provisioning_step = Server.ProvisioningStep.FAILED
            server.provisioning_error = str(error)[:2000] or "Provisioning failed."
            server.save(update_fields=["provisioning_step", "provisioning_error"])
            try:
                server.transition_to(Server.Status.FAILED)
            except ValidationError:
                pass
        if isinstance(error, DockerServiceError):
            return
        raise
    finally:
        close_old_connections()


def start_server(server, docker_service=None):
    docker_service = docker_service or DockerService()
    reprovision = False
    with transaction.atomic():
        server = Server.objects.select_for_update().get(pk=server.pk)
        if server.status == Server.Status.FAILED:
            # Rebuild instead of restarting the old container: the previous
            # attempt may have used stale limits or a broken entrypoint.
            if server.container_id:
                try:
                    docker_service.remove_container(server.container_id)
                except DockerServiceError as error:
                    raise ServerServiceError(str(error)) from error
            server.container_id = ""
            server.provisioning_step = Server.ProvisioningStep.ALLOCATING_PORT
            server.provisioning_error = ""
            server.save(update_fields=["container_id", "provisioning_step", "provisioning_error"])
            server.transition_to(Server.Status.CREATING)
            reprovision = True
        else:
            if server.status != Server.Status.STOPPED:
                raise InvalidServerAction("Only stopped or failed servers can be started.")
            try:
                docker_service.start_container(
                    server.container_id,
                    wait_for_php=(server.image == Server.Image.PHP),
                    server=server,
                )
            except DockerServiceError as error:
                raise ServerServiceError(str(error)) from error
            server.transition_to(Server.Status.RUNNING)
    if reprovision:
        _spawn_provisioning(server.pk, docker_service)
    return server


def stop_server(server, docker_service=None):
    docker_service = docker_service or DockerService()
    with transaction.atomic():
        server = Server.objects.select_for_update().get(pk=server.pk)
        if server.status != Server.Status.RUNNING:
            raise InvalidServerAction("Only running servers can be stopped.")
        try:
            docker_service.stop_container(server.container_id)
        except DockerServiceError as error:
            raise ServerServiceError(str(error)) from error
        server.transition_to(Server.Status.STOPPED)
    return server


def restart_server(server, docker_service=None):
    docker_service = docker_service or DockerService()
    with transaction.atomic():
        server = Server.objects.select_for_update().get(pk=server.pk)
        if server.status == Server.Status.STOPPED:
            return start_server(server, docker_service=docker_service)
        if server.status != Server.Status.RUNNING:
            raise InvalidServerAction("Only running or stopped servers can be restarted.")
        try:
            docker_service.restart_container(
                server.container_id,
                wait_for_php=(server.image == Server.Image.PHP),
                server=server,
            )
        except DockerServiceError as error:
            raise ServerServiceError(str(error)) from error
    return server


def delete_server(server, docker_service=None):
    docker_service = docker_service or DockerService()
    with transaction.atomic():
        server = Server.objects.select_for_update().get(pk=server.pk)
        if server.status == Server.Status.DELETED:
            return server
        if server.container_id:
            try:
                docker_service.remove_container(server.container_id)
            except DockerServiceError as error:
                raise ServerServiceError(str(error)) from error
        server.mark_deleted()
        released = []
        if server.host_port is not None:
            server.host_port = None
            released.append("host_port")
        if server.ssh_port is not None:
            server.ssh_port = None
            released.append("ssh_port")
        if released:
            server.save(update_fields=released)
    return server


def create_server_ssh_key(server, *, label, public_key="", allowed_from="", docker_service=None):
    """Add one key to a server and return the private half when MiniCloud generated the pair.

    The private key exists only in this return value: nothing on disk or in the
    database keeps a copy, so it has to be handed to the user immediately.
    """
    generated = generate_key_pair(label) if not (public_key or "").strip() else None
    key = ServerSSHKey(
        server=server,
        label=" ".join((label or "").split()),
        public_key=generated["public_key"] if generated else Server.parse_ssh_public_key(public_key),
        allowed_from=Server.parse_ssh_allowed_from(allowed_from),
    )
    key.full_clean()
    with transaction.atomic():
        key.save()
        _apply_ssh_keys(server, docker_service)
    return key, (generated["private_key"] if generated else "")


def delete_server_ssh_key(server, key, docker_service=None):
    with transaction.atomic():
        key.delete()
        _apply_ssh_keys(server, docker_service)


def _apply_ssh_keys(server, docker_service):
    """Rewrite authorized_keys in place; sshd reads it per connection, so no restart is needed."""
    if not server.ssh_enabled or server.status != Server.Status.RUNNING:
        return
    (docker_service or DockerService()).configure_ssh(server)


def check_ssh_reachability(server, timeout=5.0):
    """Connect to the advertised SSH endpoint and read the banner.

    A router that refuses to hairpin its own public address back into the LAN
    makes this fail even when the forward works for outside clients, so the
    result is reported as state plus message and the caller decides how to warn.
    """
    result = {
        "host": server.ssh_host,
        "port": server.ssh_port,
        "exposed": server.ssh_exposed,
        "allowed_from": server.ssh_allowed_from,
        "reachable": False,
        "state": "disabled",
        "message": "",
    }
    if not server.ssh_enabled or not server.ssh_port:
        result["message"] = "SSH access is not enabled for this server."
        return result
    if server.status != Server.Status.RUNNING:
        result["state"] = "stopped"
        result["message"] = "The server is not running, so sshd is not listening yet."
        return result

    banner = ""
    try:
        with socket.create_connection((result["host"], server.ssh_port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            try:
                banner = connection.recv(128).decode("utf-8", "replace")
            except OSError:
                banner = ""
    except socket.gaierror as error:
        result["state"] = "unresolved"
        result["message"] = f"{result['host']} does not resolve yet. Wait for the DDNS record to propagate, or set MINICLOUD_SSH_HOST to your public IP."
        return _note_hairpin(result)
    except ConnectionRefusedError:
        result["state"] = "refused"
        result["message"] = f"{result['host']}:{server.ssh_port} refused the connection. The forward is missing, or it points at the wrong machine or port."
        return _note_hairpin(result)
    except TimeoutError:
        result["state"] = "timeout"
        result["message"] = f"{result['host']}:{server.ssh_port} did not answer within {int(timeout)}s. Check the router's port forward and this machine's firewall."
        return _note_hairpin(result)
    except OSError as error:
        result["state"] = "unreachable"
        result["message"] = f"{result['host']}:{server.ssh_port} could not be reached: {error}."
        return _note_hairpin(result)

    first_line = banner.strip().split("\r\n")[0].split("\n")[0]
    if first_line.startswith("SSH-"):
        result["reachable"] = True
        result["state"] = "open"
        result["message"] = f"sshd answered with {first_line}."
        return result

    result["state"] = "not_ssh"
    result["message"] = (
        f"Something on {result['host']}:{server.ssh_port} accepted the connection but is not sshd"
        + (f" (it said {first_line!r})." if first_line else ".")
        + " The forward probably points at the application port instead of the SSH port."
    )
    return result


def _note_hairpin(result):
    if result["exposed"]:
        result["message"] += " Routers rarely loop a forwarded port back to the LAN, so test again from outside your network before you conclude it is broken."
    return result
