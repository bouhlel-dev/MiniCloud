from django.urls import re_path

from servers.consumers import ServerTerminalConsumer

from .consumers import NotificationConsumer, ServerLogsConsumer, ServerMetricsConsumer


websocket_urlpatterns = [
    re_path(r"ws/notifications/$", NotificationConsumer.as_asgi()),
    re_path(r"ws/servers/(?P<pk>[0-9]+)/metrics/$", ServerMetricsConsumer.as_asgi()),
    re_path(r"ws/servers/(?P<pk>[0-9]+)/logs/$", ServerLogsConsumer.as_asgi()),
    re_path(r"ws/servers/(?P<pk>[0-9]+)/terminal/$", ServerTerminalConsumer.as_asgi()),
]
