from django.core.exceptions import ValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from monitoring.models import Incident, MetricSnapshot, Notification
from monitoring.tasks import check_server_health
from servers import database_admin
from servers.database_admin import DatabaseAdminError
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

from .serializers import (
    MetricSnapshotSerializer,
    NotificationSerializer,
    ServerSerializer,
    ServerTemplateSerializer,
)


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

    @action(detail=True, methods=["get"], url_path="database")
    def database_overview(self, request, pk=None):
        server = self.get_object()
        database = request.query_params.get("database") or None
        try:
            return Response(database_admin.get_overview(server, database))
        except DatabaseAdminError as error:
            return self._database_error(error)

    @action(detail=True, methods=["post"], url_path="database/query")
    def database_query(self, request, pk=None):
        server = self.get_object()
        try:
            return Response(
                database_admin.run_query(
                    server,
                    request.data.get("sql", ""),
                    request.data.get("database") or None,
                )
            )
        except DatabaseAdminError as error:
            return self._database_error(error)

    @action(detail=True, methods=["post"], url_path="database/tables")
    def database_create_table(self, request, pk=None):
        server = self.get_object()
        try:
            table = database_admin.create_table(
                server,
                request.data.get("schema") or "public",
                request.data.get("name", ""),
                request.data.get("columns"),
                request.data.get("database") or None,
            )
        except DatabaseAdminError as error:
            return self._database_error(error)
        return Response(table, status=status.HTTP_201_CREATED)

    @action(
        detail=True,
        methods=["get", "delete"],
        url_path=r"database/tables/(?P<schema>[^/]+)/(?P<table>[^/]+)",
    )
    def database_table(self, request, pk=None, schema=None, table=None):
        server = self.get_object()
        database = request.query_params.get("database") or None
        if request.method == "DELETE":
            try:
                return Response(database_admin.drop_table(server, schema, table, database))
            except DatabaseAdminError as error:
                return self._database_error(error)
        try:
            limit = request.query_params.get("limit", 50)
            offset = request.query_params.get("offset", 0)
            return Response(database_admin.get_table(server, schema, table, database, limit, offset))
        except (DatabaseAdminError, ValueError) as error:
            return self._database_error(error)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"database/tables/(?P<schema>[^/]+)/(?P<table>[^/]+)/rows",
    )
    def database_insert_row(self, request, pk=None, schema=None, table=None):
        server = self.get_object()
        try:
            return Response(
                database_admin.insert_row(
                    server,
                    schema,
                    table,
                    request.data.get("values") or {},
                    request.data.get("database") or None,
                ),
                status=status.HTTP_201_CREATED,
            )
        except DatabaseAdminError as error:
            return self._database_error(error)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"database/tables/(?P<schema>[^/]+)/(?P<table>[^/]+)/rows/update",
    )
    def database_update_row(self, request, pk=None, schema=None, table=None):
        server = self.get_object()
        try:
            return Response(
                database_admin.update_row(
                    server,
                    schema,
                    table,
                    request.data.get("key") or {},
                    request.data.get("values") or {},
                    request.data.get("database") or None,
                )
            )
        except DatabaseAdminError as error:
            return self._database_error(error)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"database/tables/(?P<schema>[^/]+)/(?P<table>[^/]+)/rows/delete",
    )
    def database_delete_row(self, request, pk=None, schema=None, table=None):
        server = self.get_object()
        try:
            return Response(
                database_admin.delete_row(
                    server,
                    schema,
                    table,
                    request.data.get("key") or {},
                    request.data.get("database") or None,
                )
            )
        except DatabaseAdminError as error:
            return self._database_error(error)

    @action(
        detail=True,
        methods=["post"],
        url_path=r"database/tables/(?P<schema>[^/]+)/(?P<table>[^/]+)/truncate",
    )
    def database_truncate_table(self, request, pk=None, schema=None, table=None):
        server = self.get_object()
        try:
            return Response(
                database_admin.truncate_table(server, schema, table, request.data.get("database") or None)
            )
        except DatabaseAdminError as error:
            return self._database_error(error)

    @action(detail=False, methods=["post"])
    def run_health_check(self, request):
        result = check_server_health()
        return Response(result)

    @action(detail=True, methods=["post"])
    def trigger_incident(self, request, pk=None):
        server = self.get_object()
        incident_type = request.data.get("type")
        if incident_type not in Incident.Type.values:
            return Response({"detail": "Invalid incident type."}, status=status.HTTP_400_BAD_REQUEST)

        from monitoring.services import create_notification, open_incident

        incident, created = open_incident(server, incident_type)
        if not created:
            create_notification(
                server.owner,
                (
                    f"{server.name} already has an open {incident.get_type_display().lower()} incident, "
                    "so no duplicate was raised. Resolve it from the server page or wait for the health check."
                ),
                level="warning",
                server=server,
                kind=Notification.Kind.INCIDENT_OPENED,
                title=f"{incident.get_type_display()} still active",
            )
        return Response({"detail": f"Artificial incident '{incident_type}' triggered."})

    @staticmethod
    def _database_error(error):
        message = str(error)
        return Response({"detail": message}, status=status.HTTP_400_BAD_REQUEST)

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


class NotificationViewSet(viewsets.ModelViewSet):
    serializer_class = NotificationSerializer

    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user).select_related("server")

    @action(detail=True, methods=["post"])
    def mark_read(self, request, pk=None):
        notification = self.get_object()
        if not notification.is_read:
            notification.is_read = True
            notification.save(update_fields=["is_read"])
        return Response(self.get_serializer(notification).data)

    @action(detail=False, methods=["post"])
    def mark_all_read(self, request):
        Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
        return Response({"detail": "All notifications marked as read."})
