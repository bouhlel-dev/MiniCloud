from django.urls import path
from rest_framework.routers import DefaultRouter
from rest_framework.authtoken.views import obtain_auth_token

from .views import ServerTemplateViewSet, ServerViewSet


router = DefaultRouter()
router.register("servers", ServerViewSet, basename="api-server")
router.register("templates", ServerTemplateViewSet, basename="api-template")

urlpatterns = [
	*router.urls,
	path("token/", obtain_auth_token, name="api-token"),
]
