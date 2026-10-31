from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token

from .views import NotificationViewSet, ServerTemplateViewSet, ServerViewSet


router = DefaultRouter()
router.register("servers", ServerViewSet, basename="api-server")
router.register("templates", ServerTemplateViewSet, basename="api-template")
router.register("notifications", NotificationViewSet, basename="api-notification")

urlpatterns = [
	*router.urls,
	path("token/", obtain_auth_token, name="api-token"),
]
