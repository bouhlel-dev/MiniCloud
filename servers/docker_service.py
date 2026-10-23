import base64
import shlex
import socket
from datetime import datetime, timezone
from decimal import Decimal
from textwrap import dedent

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from django.conf import settings

from .models import Server


class DockerServiceError(Exception):
    pass


class DockerContainerNotFound(DockerServiceError):
    pass


class DockerTerminal:
    _PATH_MARKER = "__MINICLOUD_CWD__"

    def __init__(self, api_client, exec_id, socket_handle, supports_path_reporting=True):
        self._api_client = api_client
        self._exec_id = exec_id
        self._socket_handle = socket_handle
        self.supports_path_reporting = supports_path_reporting

    def read(self, size=4096):
        return self._socket_handle.recv(size)

    def write(self, data):
        self._socket_handle.sendall(data)

    def enable_path_reporting(self):
        if not self.supports_path_reporting:
            return
        command = (
            "PS1=$(printf '%s%s%s' '__MINICLOUD_' "
            "'CWD__$(pwd)__MINICLOUD_' 'CWD__$ ')\n"
        )
        self.write(command.encode("utf-8"))

    def resize(self, columns, rows):
        self._api_client.exec_resize(self._exec_id, height=rows, width=columns)

    def close(self):
        self._socket_handle.close()


class DockerService:
    IMAGE_SPECS = {
        Server.Image.NGINX: {"image": "nginx:alpine", "container_port": "80/tcp"},
        Server.Image.PYTHON: {
            "image": "python:3.12-slim",
            "command": ["sh", "-c", "tail -f /dev/null"],
            "container_port": "8000/tcp",
        },
        Server.Image.NODE: {
            "image": "node:22-alpine",
            "command": ["sh", "-c", "tail -f /dev/null"],
            "container_port": "3000/tcp",
        },
        Server.Image.PHP: {"image": "debian:bookworm-slim", "container_port": "80/tcp"},
        Server.Image.POSTGRES: {"image": "postgres:16-alpine", "container_port": "5432/tcp"},
    }
    PHP_XAMPP_COMMAND = """
        set -eu
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends ca-certificates curl nano procps wget
        rm -rf /var/lib/apt/lists/*
        installer_url={installer_url}
        echo "MINICLOUD_XAMPP_DOWNLOADING: Downloading XAMPP installer from $installer_url"
        for i in 1 2 3; do
            if wget --timeout=120 --tries=1 --no-verbose --output-document=/tmp/xampp-installer.run "$installer_url"; then
                break
            fi
            echo "Download attempt $i failed, retrying..."
            sleep 5
        done
        if [ ! -f /tmp/xampp-installer.run ] || [ ! -s /tmp/xampp-installer.run ]; then
            echo "MINICLOUD_XAMPP_FAILED: could not download the XAMPP installer" >&2
            exit 1
        fi
        chmod +x /tmp/xampp-installer.run
        echo "MINICLOUD_XAMPP_INSTALLING: Installing XAMPP..."
        /tmp/xampp-installer.run --mode unattended
        rm -f /tmp/xampp-installer.run
        echo "MINICLOUD_XAMPP_STARTING: Starting XAMPP (Apache + MariaDB + PHP + Perl)..."
        /opt/lampp/lampp start
        ready=0
        for i in 1 2 3 4 5 6 7 8 9 10; do
            if curl -f -s -o /dev/null http://localhost:80; then
                ready=1
                break
            fi
            echo "Waiting for Apache to start... ($i/10)"
            sleep 3
        done
        if [ "$ready" = "1" ]; then
            echo "MINICLOUD_XAMPP_READY: XAMPP is serving on port 80"
        else
            echo "MINICLOUD_XAMPP_FAILED: Apache did not start" >&2
        fi
        trap '/opt/lampp/lampp stop || true; exit 0' INT TERM
        while true; do
            sleep 3600 & wait $!
        done
    """
    FRAMEWORK_COMMANDS = {
        (Server.Image.PYTHON, Server.Framework.FASTAPI): """
            pip install --disable-pip-version-check --no-cache-dir fastapi uvicorn
            mkdir -p /workspace
            if [ ! -f /workspace/main.py ]; then
                cat > /workspace/main.py <<'PY'
from fastapi import FastAPI

app = FastAPI()


@app.get("/")
def read_root():
    return {"message": "FastAPI is running on MiniCloud"}
PY
            fi
            exec uvicorn main:app --app-dir /workspace --host 0.0.0.0 --port 8000
        """,
        (Server.Image.PYTHON, Server.Framework.FLASK): """
            pip install --disable-pip-version-check --no-cache-dir flask
            mkdir -p /workspace
            if [ ! -f /workspace/app.py ]; then
                cat > /workspace/app.py <<'PY'
from flask import Flask

app = Flask(__name__)


@app.get("/")
def index():
    return {"message": "Flask is running on MiniCloud"}
PY
            fi
            exec flask --app /workspace/app run --host 0.0.0.0 --port 8000
        """,
        (Server.Image.PYTHON, Server.Framework.DJANGO): """
            pip install --disable-pip-version-check --no-cache-dir django
            mkdir -p /workspace
            if [ ! -f /workspace/manage.py ]; then
                django-admin startproject config /workspace
            fi
            cd /workspace
            python manage.py migrate --noinput
            exec python manage.py runserver 0.0.0.0:8000
        """,
        (Server.Image.NODE, Server.Framework.EXPRESS): """
            mkdir -p /workspace
            if [ ! -f /workspace/package.json ]; then
                cat > /workspace/package.json <<'JSON'
{"name":"minicloud-express","private":true,"scripts":{"start":"node index.js"}}
JSON
            fi
            if [ ! -f /workspace/index.js ]; then
                cat > /workspace/index.js <<'JS'
const express = require("express");
const app = express();

app.get("/", (_request, response) => response.json({ message: "Express is running on MiniCloud" }));
app.listen(3000, "0.0.0.0");
JS
            fi
            cd /workspace
            npm install --no-audit --no-fund --omit=dev express
            exec npm start
        """,
        (Server.Image.NODE, Server.Framework.NEXTJS): """
            mkdir -p /workspace/app
            if [ ! -f /workspace/package.json ]; then
                cat > /workspace/package.json <<'JSON'
{"name":"minicloud-nextjs","private":true,"scripts":{"dev":"next dev --hostname 0.0.0.0 --port 3000"}}
JSON
            fi
            if [ ! -f /workspace/app/layout.js ]; then
                cat > /workspace/app/layout.js <<'JS'
export default function RootLayout({ children }) {
  return <html><body>{children}</body></html>;
}
JS
            fi
            if [ ! -f /workspace/app/page.js ]; then
                cat > /workspace/app/page.js <<'JS'
export default function Home() {
  return <main><h1>Next.js is running on MiniCloud</h1></main>;
}
JS
            fi
            cd /workspace
            npm install --no-audit --no-fund --omit=dev next react react-dom
            exec npm run dev
        """,
    }
    NANO_INSTALL_COMMAND = """
        set -eu
        if command -v nano >/dev/null 2>&1; then
            exit 0
        elif command -v apk >/dev/null 2>&1; then
            apk add --no-cache nano
        elif command -v apt-get >/dev/null 2>&1; then
            export DEBIAN_FRONTEND=noninteractive
            apt-get update
            apt-get install -y --no-install-recommends nano
            rm -rf /var/lib/apt/lists/*
        elif command -v dnf >/dev/null 2>&1; then
            dnf install -y nano
        elif command -v yum >/dev/null 2>&1; then
            yum install -y nano
        else
            echo "No supported package manager was found to install nano." >&2
            exit 127
        fi
    """

    def __init__(self, client=None):
        self._client = client

    @property
    def client(self):
        if self._client is None:
            try:
                self._client = docker.from_env(timeout=settings.MINICLOUD_DOCKER_TIMEOUT)
            except DockerException as error:
                raise DockerServiceError("Docker is unavailable. Check that the local Docker daemon is running.") from error
        return self._client

    def find_available_host_port(self):
        assigned_ports = self._assigned_host_ports()
        for port in range(settings.MINICLOUD_PORT_RANGE_START, settings.MINICLOUD_PORT_RANGE_END + 1):
            if port not in assigned_ports and self._is_locally_available(port):
                return port
        raise DockerServiceError("No host ports are available in the configured MiniCloud port range.")

    def _update_step(self, server, step):
        server.provisioning_step = step
        if server.pk:
            server.save(update_fields=["provisioning_step"])

    def create_container(self, server):
        image_spec = self._image_spec(server)
        image_name = image_spec["image"]
        
        self._update_step(server, Server.ProvisioningStep.PULLING_IMAGE)
        try:
            self.client.images.get(image_name)
        except ImageNotFound:
            try:
                self.client.images.pull(image_name)
            except DockerException as error:
                raise DockerServiceError("Docker could not pull the selected image.") from error
        except DockerException as error:
            raise DockerServiceError("Docker could not inspect the selected image.") from error

        if not server.host_port:
            raise DockerServiceError("A host port is required before creating a container.")

        self._update_step(server, Server.ProvisioningStep.CREATING_CONTAINER)
        run_options = {
            "detach": True,
            "name": f"minicloud-{server.pk}",
            "environment": {key: str(value) for key, value in server.env_vars.items()},
            "ports": {image_spec["container_port"]: server.host_port},
            "mem_limit": int(server.template.memory_mb) * 1024 * 1024,
            "nano_cpus": int(Decimal(str(server.template.cpu_limit)) * Decimal("1000000000")),
            "labels": {
                "minicloud.managed": "true",
                "minicloud.server_id": str(server.pk),
            },
        }
        if server.image == Server.Image.POSTGRES and server.database_size_mb:
            run_options["storage_opt"] = {"size": f"{server.database_size_mb}m"}
        if server.image == Server.Image.PHP:
            self._update_step(server, Server.ProvisioningStep.DOWNLOADING_XAMPP)
            run_options["command"] = ["sh", "-c", self._php_xampp_command()]
        elif framework_command := self._framework_command(server):
            self._update_step(server, Server.ProvisioningStep.INSTALLING_DEPENDENCIES)
            run_options["command"] = ["sh", "-c", framework_command]
        elif server.image == Server.Image.PYTHON:
            self._update_step(server, Server.ProvisioningStep.CONFIGURING)
            page = base64.b64encode(self._live_page(server).encode()).decode("ascii")
            run_options["command"] = [
                "sh", "-c",
                f"mkdir -p /tmp/minicloud-site && echo {page} | base64 -d > /tmp/minicloud-site/index.html && exec python -m http.server 8000 --bind 0.0.0.0 --directory /tmp/minicloud-site",
            ]
        elif server.image == Server.Image.NODE:
            self._update_step(server, Server.ProvisioningStep.CONFIGURING)
            page = base64.b64encode(self._live_page(server).encode()).decode("ascii")
            run_options["command"] = [
                "node", "-e",
                f"const http=require('http');const page=Buffer.from('{page}','base64').toString();http.createServer((r,s)=>s.end(page)).listen(3000,'0.0.0.0')",
            ]
        elif "command" in image_spec:
            run_options["command"] = image_spec["command"]

        try:
            container = self.client.containers.run(image_name, **run_options)
        except APIError as error:
            if server.image == Server.Image.POSTGRES and server.database_size_mb:
                raise DockerServiceError(
                    "Docker could not apply the PostgreSQL storage limit. "
                    "Configure a storage driver that supports writable-layer quotas or choose no limit."
                ) from error
            raise DockerServiceError("Docker could not create the container.") from error
        except DockerException as error:
            raise DockerServiceError("Docker could not create the container.") from error

        if server.pk:
            server.container_id = container.id
            server.save(update_fields=["container_id"])

        if server.image == Server.Image.NGINX:
            self._update_step(server, Server.ProvisioningStep.CONFIGURING)
            encoded = base64.b64encode(self._live_page(server).encode()).decode("ascii")
            try:
                result = container.exec_run(
                    ["sh", "-c", f"echo {encoded} | base64 -d > /usr/share/nginx/html/index.html"]
                )
            except DockerException as error:
                raise DockerServiceError("Docker could not write the static website content.") from error
            if result.exit_code != 0:
                raise DockerServiceError("Docker could not write the server liveness page.")
        if server.image != Server.Image.PHP:
            try:
                self._install_nano(container)
            except DockerServiceError:
                try:
                    container.remove(force=True)
                except DockerException:
                    pass
                raise
        if server.image == Server.Image.PHP:
            self._wait_for_xampp_ready(container, server=server)
        return container.id

    @staticmethod
    def _live_page(server):
        return f"<!doctype html><html><head><meta charset='utf-8'><title>MiniCloud</title></head><body><h1>Server is live</h1><p>{server.name} is running on MiniCloud.</p></body></html>"

    @staticmethod
    def _php_xampp_command():
        installer_url = shlex.quote(settings.MINICLOUD_XAMPP_INSTALLER_URL)
        return dedent(DockerService.PHP_XAMPP_COMMAND).strip().format(installer_url=installer_url)

    def _wait_for_xampp_ready(self, container, server=None, max_wait=None, interval=3):
        """Track XAMPP setup from container output and wait until Apache is serving."""
        import time
        if max_wait is None:
            max_wait = getattr(settings, "MINICLOUD_XAMPP_READY_TIMEOUT", 1800)
        markers = (
            ("MINICLOUD_XAMPP_DOWNLOADING", Server.ProvisioningStep.DOWNLOADING_XAMPP),
            ("MINICLOUD_XAMPP_INSTALLING", Server.ProvisioningStep.INSTALLING_XAMPP),
            ("MINICLOUD_XAMPP_STARTING", Server.ProvisioningStep.STARTING_APACHE),
            ("MINICLOUD_XAMPP_READY", Server.ProvisioningStep.READY),
            ("MINICLOUD_XAMPP_FAILED", Server.ProvisioningStep.FAILED),
        )
        last_step = None
        start_time = time.time()
        while time.time() - start_time < max_wait:
            try:
                raw = container.logs(tail=400)
                logs = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
            except DockerException:
                logs = ""
            latest_marker, latest_step, latest_pos = None, None, -1
            for marker, step in markers:
                pos = logs.rfind(marker)
                if pos > latest_pos:
                    latest_marker, latest_step, latest_pos = marker, step, pos
            if latest_step and server is not None and server.pk and latest_step != last_step:
                if latest_step not in {Server.ProvisioningStep.READY, Server.ProvisioningStep.FAILED}:
                    last_step = latest_step
                    self._update_step(server, latest_step)
            if latest_marker == "MINICLOUD_XAMPP_READY":
                return True
            if latest_marker == "MINICLOUD_XAMPP_FAILED":
                raise DockerServiceError("XAMPP setup failed. Check the container logs for details.")
            try:
                container.reload()
                if container.status not in {"running", "created"}:
                    raise DockerServiceError("The XAMPP container stopped before it became ready.")
            except DockerException:
                pass
            time.sleep(interval)
        raise DockerServiceError("XAMPP did not become ready in time. Check container logs for details.")

    def start_container(self, container_id, wait_for_php=False):
        self._run_container_action(container_id, "start")
        if wait_for_php:
            container = self._get_container(container_id)
            self._wait_for_xampp_ready(container)

    def stop_container(self, container_id):
        self._run_container_action(container_id, "stop", timeout=10)

    def restart_container(self, container_id):
        self._run_container_action(container_id, "restart", timeout=10)

    def remove_container(self, container_id):
        try:
            self._get_container(container_id).remove(force=True)
        except DockerServiceError as error:
            if str(error) != "The Docker container no longer exists.":
                raise
        except DockerException as error:
            raise DockerServiceError("Docker could not remove the container.") from error

    def get_container_status(self, container_id):
        container = self._get_container(container_id)
        try:
            container.reload()
        except DockerException as error:
            raise DockerServiceError("Docker could not inspect the container status.") from error
        return container.status

    def get_container_stats(self, container_id):
        container = self._get_container(container_id)
        try:
            stats = container.stats(stream=False)
        except DockerServiceError:
            raise
        except DockerException as error:
            raise DockerServiceError("Docker could not collect container statistics.") from error

        cpu_stats = stats.get("cpu_stats", {})
        previous_cpu_stats = stats.get("precpu_stats", {})
        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - previous_cpu_stats.get("cpu_usage", {}).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - previous_cpu_stats.get("system_cpu_usage", 0)
        online_cpus = cpu_stats.get("online_cpus") or len(cpu_stats.get("cpu_usage", {}).get("percpu_usage") or []) or 1
        cpu_percent = (cpu_delta / system_delta) * online_cpus * 100 if system_delta > 0 and cpu_delta >= 0 else 0

        memory_stats = stats.get("memory_stats", {})
        memory_mb = memory_stats.get("usage", 0) / (1024 * 1024)
        network_stats = stats.get("networks", {})
        net_rx = sum(network.get("rx_bytes", 0) for network in network_stats.values())
        net_tx = sum(network.get("tx_bytes", 0) for network in network_stats.values())
        started_at = container.attrs.get("State", {}).get("StartedAt")
        uptime_seconds = self._uptime_seconds(started_at)
        return {
            "cpu_percent": cpu_percent,
            "memory_mb": memory_mb,
            "net_rx": net_rx,
            "net_tx": net_tx,
            "uptime_seconds": uptime_seconds,
            "uptime_display": self._format_uptime(uptime_seconds),
        }

    @staticmethod
    def _uptime_seconds(started_at):
        if not started_at or started_at.startswith("0001-01-01"):
            return 0
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return 0
        return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))

    @staticmethod
    def _format_uptime(seconds):
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"

    def get_container_logs(self, container_id, tail=100):
        try:
            logs = self._get_container(container_id).logs(tail=tail, timestamps=True)
        except DockerServiceError:
            raise
        except DockerException as error:
            raise DockerServiceError("Docker could not read the container logs.") from error
        return logs.decode("utf-8", errors="replace") if isinstance(logs, bytes) else str(logs)

    def execute_container_command(self, container_id, command):
        if not command or len(command) > 2000:
            raise DockerServiceError("Commands must contain between 1 and 2000 characters.")
        try:
            result = self._get_container(container_id).exec_run(
                ["sh", "-lc", command],
                stdout=True,
                stderr=True,
            )
        except DockerServiceError:
            raise
        except DockerException as error:
            raise DockerServiceError("Docker could not execute the container command.") from error
        output = result.output
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {"exit_code": result.exit_code, "output": output or ""}

    def open_terminal(self, container_id, *, command=None, environment=None, supports_path_reporting=True):
        container = self._get_container(container_id)
        try:
            container.reload()
            if container.status != "running":
                raise DockerServiceError("An interactive shell requires a running container.")
            exec_options = {
                "cmd": command or ["/bin/sh", "-l"],
                "stdin": True,
                "stdout": True,
                "stderr": True,
                "tty": True,
            }
            if environment:
                exec_options["environment"] = environment
            execution = self.client.api.exec_create(
                container.id,
                **exec_options,
            )
            socket_handle = self.client.api.exec_start(execution["Id"], tty=True, socket=True)
        except DockerServiceError:
            raise
        except DockerException as error:
            raise DockerServiceError("Docker could not open an interactive shell in the container.") from error
        return DockerTerminal(
            self.client.api,
            execution["Id"],
            socket_handle,
            supports_path_reporting=supports_path_reporting,
        )

    def _image_spec(self, server):
        if server.image == Server.Image.CUSTOM:
            if not server.custom_image:
                raise DockerServiceError("A Docker image is required for custom servers.")
            return {"image": server.custom_image, "container_port": "80/tcp"}
        try:
            image_spec = self.IMAGE_SPECS[server.image].copy()
        except KeyError as error:
            raise DockerServiceError("The selected server image is not supported.") from error
        if server.image == Server.Image.POSTGRES and not server.env_vars.get("POSTGRES_PASSWORD"):
            raise DockerServiceError("PostgreSQL servers require POSTGRES_PASSWORD in environment variables.")
        if not Server.supports_framework(server.image, server.framework):
            raise DockerServiceError("The selected framework is not compatible with this server image.")
        return image_spec

    def _framework_command(self, server):
        command = self.FRAMEWORK_COMMANDS.get((server.image, server.framework))
        return dedent(command).strip() if command else None

    def _install_nano(self, container):
        try:
            result = container.exec_run(
                ["sh", "-c", dedent(self.NANO_INSTALL_COMMAND).strip()],
                stdout=True,
                stderr=True,
                user="root",
            )
        except DockerException as error:
            raise DockerServiceError("Docker could not install nano in the container.") from error
        if result.exit_code != 0:
            raise DockerServiceError("Nano could not be installed in the container.")

    def _assigned_host_ports(self):
        assigned_ports = set(
            Server.objects.exclude(status=Server.Status.DELETED)
            .exclude(host_port__isnull=True)
            .values_list("host_port", flat=True)
        )
        try:
            containers = self.client.containers.list(all=True)
        except DockerException as error:
            raise DockerServiceError("Docker could not inspect allocated host ports.") from error

        for container in containers:
            port_bindings = container.attrs.get("NetworkSettings", {}).get("Ports") or {}
            for bindings in port_bindings.values():
                for binding in bindings or []:
                    if host_port := binding.get("HostPort"):
                        assigned_ports.add(int(host_port))
        return assigned_ports

    @staticmethod
    def _is_locally_available(port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as socket_handle:
            socket_handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                socket_handle.bind(("0.0.0.0", port))
            except OSError:
                return False
        return True

    def _get_container(self, container_id):
        if not container_id:
            raise DockerServiceError("This server does not have a Docker container.")
        try:
            return self.client.containers.get(container_id)
        except NotFound as error:
            raise DockerContainerNotFound("The Docker container no longer exists.") from error
        except (APIError, DockerException) as error:
            raise DockerServiceError("Docker could not access the container.") from error

    def _run_container_action(self, container_id, action, **kwargs):
        try:
            getattr(self._get_container(container_id), action)(**kwargs)
        except DockerServiceError:
            raise
        except DockerException as error:
            raise DockerServiceError(f"Docker could not {action} the container.") from error
