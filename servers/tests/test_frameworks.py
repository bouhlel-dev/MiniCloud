import pytest
from django.core.exceptions import ValidationError
from unittest.mock import Mock

from servers.docker_service import DockerService
from servers.models import Server, ServerTemplate
from accounts.models import User


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="framework-owner", password="password")


@pytest.fixture
def template(db):
    return ServerTemplate.objects.create(name="Framework", cpu_limit="0.50", memory_mb=256)


def test_framework_must_match_server_image(owner, template):
    server = Server(
        owner=owner,
        name="invalid-framework",
        image=Server.Image.PYTHON,
        framework=Server.Framework.EXPRESS,
        template=template,
    )

    with pytest.raises(ValidationError, match="compatible"):
        server.full_clean()


@pytest.mark.parametrize(
    ("image", "framework", "dependency", "launch_command"),
    [
        (Server.Image.PYTHON, Server.Framework.FASTAPI, "fastapi uvicorn", "exec uvicorn"),
        (Server.Image.PYTHON, Server.Framework.FLASK, "flask", "exec flask"),
        (Server.Image.PYTHON, Server.Framework.DJANGO, "django", "manage.py runserver"),
        (Server.Image.NODE, Server.Framework.EXPRESS, "express", "exec npm start"),
        (Server.Image.NODE, Server.Framework.NEXTJS, "next react react-dom", "exec npm run dev"),
    ],
)
def test_framework_command_installs_dependencies_and_starts_app(owner, template, image, framework, dependency, launch_command):
    server = Server(
        owner=owner,
        name=f"{framework}-server",
        image=image,
        framework=framework,
        template=template,
    )

    command = DockerService()._framework_command(server)

    assert dependency in command
    assert "/workspace" in command
    assert launch_command in command


@pytest.mark.parametrize("image", [Server.Image.PYTHON, Server.Image.NODE])
def test_blank_runtime_does_not_scaffold_a_framework(owner, template, image):
    server = Server(
        owner=owner,
        name=f"blank-{image}",
        image=image,
        template=template,
    )

    assert DockerService()._framework_command(server) is None


def test_nano_is_installed_as_root_in_new_containers():
    container = Mock()
    container.exec_run.return_value.exit_code = 0

    DockerService()._install_nano(container)

    command = container.exec_run.call_args.args[0]
    assert command[:2] == ["sh", "-c"]
    assert "nano" in command[2]
    assert container.exec_run.call_args.kwargs["user"] == "root"
