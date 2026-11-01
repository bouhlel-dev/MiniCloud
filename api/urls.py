from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token

from .views import NotificationViewSet, ServerSSHKeyViewSet, ServerTemplateViewSet, ServerViewSet


router = DefaultRouter()
router.register("servers", ServerViewSet, basename="api-server")
router.register("templates", ServerTemplateViewSet, basename="api-template")
router.register("notifications", NotificationViewSet, basename="api-notification")

ssh_keys = ServerSSHKeyViewSet.as_view({"get": "list", "post": "create"})
ssh_key = ServerSSHKeyViewSet.as_view({"delete": "destroy"})

urlpatterns = [
	*router.urls,
	path("servers/<int:server_pk>/ssh/keys/", ssh_keys, name="api-server-ssh-keys"),
	path("servers/<int:server_pk>/ssh/keys/<int:pk>/", ssh_key, name="api-server-ssh-key"),
	path("token/", obtain_auth_token, name="api-token"),
]
