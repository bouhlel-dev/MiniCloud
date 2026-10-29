import threading
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import close_old_connections, transaction

from accounts.models import User

from .docker_service import DockerService, DockerServiceError
from .models import Server, ServerTemplate


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


def provision_server(owner, *, name, image, custom_image, template, env_vars, framework="", database_size_mb=None, docker_service=None):
    port_service = docker_service or DockerService()
    with transaction.atomic():
        locked_owner = User.objects.select_for_update().get(pk=owner.pk)
        locked_template = ServerTemplate.objects.get(pk=template.pk)
        if not locked_owner.has_capacity_for(locked_template, image=image):
            raise QuotaExceededError("Your server, CPU, or memory quota has been reached.")

        server = Server(
            owner=locked_owner,
            name=name,
            image=image,
            framework=framework,
            database_size_mb=database_size_mb,
            custom_image=custom_image,
            template=locked_template,
            host_port=port_service.find_available_host_port(),
            env_vars=env_vars,
            provisioning_step=Server.ProvisioningStep.ALLOCATING_PORT,
        )
        try:
            server.full_clean()
        except ValidationError as error:
            raise ProvisioningError("; ".join(error.messages)) from error
        server.save()

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
                docker_service.start_container(server.container_id, wait_for_php=(server.image == Server.Image.PHP))
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
            docker_service.restart_container(server.container_id)
            if server.image == Server.Image.PHP:
                container = docker_service._get_container(server.container_id)
                docker_service._wait_for_xampp_ready(container)
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
        if server.host_port is not None:
            server.host_port = None
            server.save(update_fields=["host_port"])
    return server
