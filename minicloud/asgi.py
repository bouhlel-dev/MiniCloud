import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "minicloud.settings")

from channels.auth import AuthMiddlewareStack
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application

django_asgi_app = get_asgi_application()

from minicloud.websocket_auth import TokenAuthMiddleware
from monitoring.routing import websocket_urlpatterns

application = ProtocolTypeRouter(
    {
        "http": django_asgi_app,
        "websocket": AuthMiddlewareStack(TokenAuthMiddleware(URLRouter(websocket_urlpatterns))),
    }
)
