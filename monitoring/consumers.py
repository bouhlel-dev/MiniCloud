import asyncio
from contextlib import suppress

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.utils import timezone

from servers.docker_service import DockerService, DockerServiceError
from servers.models import Server


class ServerStreamConsumer(AsyncJsonWebsocketConsumer):
    stream_kind = None

    async def connect(self):
        self.server = await self.get_owned_server()
        if self.server is None:
            await self.close(code=4404)
            return
        if not self.server.container_id:
            await self.close(code=4409)
            return
        await self.accept()
        self.stream_task = asyncio.create_task(self.stream_loop())

    async def disconnect(self, close_code):
        stream_task = getattr(self, "stream_task", None)
        if stream_task:
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task

    async def receive_json(self, content, **kwargs):
        if content.get("action") == "ping":
            await self.send_json({"type": "pong", "timestamp": timezone.now().isoformat()})

    async def stream_loop(self):
        while True:
            try:
                payload = await self.next_payload()
                await self.send_json(payload)
            except DockerServiceError as error:
                await self.send_json({"type": "error", "message": str(error)})
            except Exception:
                await self.send_json({"type": "error", "message": "The stream is temporarily unavailable."})
            await asyncio.sleep(2)

    async def next_payload(self):
        if self.stream_kind == "metrics":
            metric = await self.latest_metric()
            return {"type": "metric", "metric": metric}
        logs = await asyncio.to_thread(DockerService().get_container_logs, self.server.container_id, 100)
        return {"type": "logs", "logs": logs}

    @database_sync_to_async
    def get_owned_server(self):
        if not self.scope["user"].is_authenticated:
            return None
        return (
            Server.objects.filter(owner=self.scope["user"], pk=self.scope["url_route"]["kwargs"]["pk"])
            .exclude(status=Server.Status.DELETED)
            .first()
        )

    @database_sync_to_async
    def latest_metric(self):
        snapshot = self.server.metric_snapshots.order_by("-timestamp").first()
        if snapshot is None:
            return None
        return {
            "timestamp": snapshot.timestamp.isoformat(),
            "cpu_percent": float(snapshot.cpu_percent),
            "memory_mb": float(snapshot.memory_mb),
            "net_rx": snapshot.net_rx,
            "net_tx": snapshot.net_tx,
        }


class ServerMetricsConsumer(ServerStreamConsumer):
    stream_kind = "metrics"


class ServerLogsConsumer(ServerStreamConsumer):
    stream_kind = "logs"
