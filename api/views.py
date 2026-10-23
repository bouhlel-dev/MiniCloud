from django.core.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from monitoring.models import MetricSnapshot
from servers.docker_service import DockerService, DockerServiceError
from servers.models import Server, ServerTemplate
from servers.services import (
    InvalidServerAction,
    ProvisioningError,
    QuotaExceededError,
    ServerServiceError,
    delete_server,
    execute_server_command,
    provision_server,
    restart_server,
    start_server,
    stop_server,
)

from .serializers import MetricSnapshotSerializer, ServerSerializer, ServerTemplateSerializer


class ServerViewSet(viewsets.ModelViewSet):
    serializer_class = ServerSerializer

    def get_queryset(self):
        return Server.objects.filter(owner=self.request.user).exclude(status=Server.Status.DELETED).select_related("template")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            server = provision_server(request.user, **serializer.validated_data)
        except QuotaExceededError as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        except DockerServiceError as error:
            return Response(
                {"detail": str(error), "code": "docker_unavailable"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except ProvisioningError as error:
            if isinstance(error.__cause__, DockerServiceError):
                return Response(
                    {"detail": str(error), "code": "docker_unavailable"},
                    status=status.HTTP_503_SERVICE_UNAVAILABLE,
                )
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        except ServerServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(server).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        server = self.get_object()
        serializer = self.get_serializer(server, data=request.data, partial=kwargs.pop("partial", False))
        serializer.is_valid(raise_exception=True)
        allowed = {"name", "custom_image", "env_vars"}
        if set(serializer.validated_data) - allowed:
            return Response({"detail": "Only name, custom_image, and env_vars can be updated."}, status=400)
        for field, value in serializer.validated_data.items():
            setattr(server, field, value)
        try:
            server.full_clean()
            server.save()
        except ValidationError as error:
            return Response(error.message_dict, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(server).data)

    def destroy(self, request, *args, **kwargs):
        try:
            server = delete_server(self.get_object())
        except ServerServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(server).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        return self._lifecycle_response(start_server, request)

    @action(detail=True, methods=["post"])
    def stop(self, request, pk=None):
        return self._lifecycle_response(stop_server, request)

    @action(detail=True, methods=["post"])
    def restart(self, request, pk=None):
        return self._lifecycle_response(restart_server, request)

    @action(detail=True, methods=["get"])
    def metrics(self, request, pk=None):
        snapshots = MetricSnapshot.objects.filter(server=self.get_object()).order_by("-timestamp")[:60]
        return Response(MetricSnapshotSerializer(reversed(list(snapshots)), many=True).data)

    @action(detail=True, methods=["get"], url_path="metrics/live")
    def live_metrics(self, request, pk=None):
        server = self.get_object()
        if server.status != Server.Status.RUNNING:
            return Response({"detail": "Live metrics require a running server."}, status=status.HTTP_409_CONFLICT)
        try:
            return Response(DockerService().get_container_stats(server.container_id))
        except DockerServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

    @action(detail=True, methods=["post"])
    def execute(self, request, pk=None):
        command = request.data.get("command", "")
        if not isinstance(command, str) or not command.strip():
            return Response({"detail": "A command is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            result = execute_server_command(self.get_object(), command.strip())
        except InvalidServerAction as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        except ServerServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    def _lifecycle_response(self, service_method, request):
        try:
            server = service_method(self.get_object())
        except InvalidServerAction as error:
            return Response({"detail": str(error)}, status=status.HTTP_409_CONFLICT)
        except ServerServiceError as error:
            return Response({"detail": str(error)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(server).data)


class ServerTemplateViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ServerTemplateSerializer
    queryset = ServerTemplate.objects.all()
