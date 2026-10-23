from unittest.mock import MagicMock

import pytest
from django.core.exceptions import ValidationError

from accounts.models import User
from servers.docker_service import DockerService
from servers.models import Server, ServerTemplate


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="storage-owner", password="password")


@pytest.fixture
def template(db):
    return ServerTemplate.objects.create(name="Storage", cpu_limit="0.50", memory_mb=256)


def test_database_storage_limit_is_only_valid_for_postgres(owner, template):
    server = Server(
        owner=owner,
        name="web-storage-limit",
        image=Server.Image.NGINX,
        database_size_mb=512,
        template=template,
    )

    with pytest.raises(ValidationError, match="PostgreSQL"):
        server.full_clean()


def test_postgres_storage_limit_is_passed_to_docker(owner, template):
    server = Server(
        owner=owner,
        name="database-storage-limit",
        image=Server.Image.POSTGRES,
        database_size_mb=1024,
        template=template,
        host_port=15432,
        env_vars={"POSTGRES_PASSWORD": "safe-password"},
    )
    container = MagicMock()
    container.id = "container-id"
    container.exec_run.return_value.exit_code = 0
    client = MagicMock()
    client.containers.run.return_value = container

    DockerService(client=client).create_container(server)

    assert client.containers.run.call_args.kwargs["storage_opt"] == {"size": "1024m"}
