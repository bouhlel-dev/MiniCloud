import asyncio
import json
from contextlib import suppress

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from .docker_service import DockerServiceError
from .models import Server
from .services import ServerServiceError, open_server_terminal


class ServerTerminalConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.server = await self.get_owned_server()
        if self.server is None:
            await self.reject_with_error("This server is unavailable.", 4404)
            return
        if self.server.status != Server.Status.RUNNING or not self.server.container_id:
            await self.reject_with_error("Start the server before opening its shell.", 4409)
            return
        try:
            self.terminal = await asyncio.to_thread(open_server_terminal, self.server)
            if self.terminal.supports_path_reporting:
                await asyncio.to_thread(self.terminal.enable_path_reporting)
        except ServerServiceError:
            await self.reject_with_error("Docker could not open an interactive shell for this container.", 3000)
            return
        await self.accept()
        self.output_task = asyncio.create_task(self.stream_output())

    async def reject_with_error(self, message, close_code):
        await self.accept()
        await self.send(text_data=json.dumps({"type": "terminal_error", "message": message}))
        await self.close(code=close_code)

    async def disconnect(self, close_code):
        terminal = getattr(self, "terminal", None)
        if terminal:
            with suppress(OSError, RuntimeError, ServerServiceError):
                await asyncio.to_thread(terminal.close)
        output_task = getattr(self, "output_task", None)
        if output_task:
            output_task.cancel()
            with suppress(asyncio.CancelledError):
                await output_task

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is not None:
            try:
                await asyncio.to_thread(self.terminal.write, bytes_data)
            except (OSError, RuntimeError, ServerServiceError):
                await self.close(code=3000)
            return
        if not text_data:
            return
        try:
            message = json.loads(text_data)
        except json.JSONDecodeError:
            return
        if message.get("type") == "resize":
            columns = _bounded_dimension(message.get("cols"), 20, 500)
            rows = _bounded_dimension(message.get("rows"), 5, 200)
            if columns and rows:
                with suppress(OSError, RuntimeError, DockerServiceError):
                    await asyncio.to_thread(self.terminal.resize, columns, rows)

    async def stream_output(self):
        try:
            while True:
                output = await asyncio.to_thread(self.terminal.read)
                if not output:
                    break
                await self.send(bytes_data=output)
            await self.close(code=1000)
        except (OSError, RuntimeError, ServerServiceError):
            await self.close(code=3000)

    @database_sync_to_async
    def get_owned_server(self):
        if not self.scope["user"].is_authenticated:
            return None
        return (
            Server.objects.filter(owner=self.scope["user"], pk=self.scope["url_route"]["kwargs"]["pk"])
            .exclude(status=Server.Status.DELETED)
            .first()
        )


def _bounded_dimension(value, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if minimum <= value <= maximum else None
