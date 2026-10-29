from rest_framework import serializers

from monitoring.models import Incident, MetricSnapshot
from servers.models import Server, ServerTemplate
from servers.services import postgres_connection_string


class ServerSerializer(serializers.ModelSerializer):
    template_name = serializers.CharField(source="template.name", read_only=True)
    template_cpu_limit = serializers.DecimalField(source="template.cpu_limit", max_digits=6, decimal_places=2, read_only=True)
    template_memory_mb = serializers.IntegerField(source="template.memory_mb", read_only=True)
    effective_cpu_limit = serializers.DecimalField(max_digits=6, decimal_places=2, read_only=True)
    effective_memory_mb = serializers.IntegerField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    image_display = serializers.CharField(source="get_image_display", read_only=True)
    framework_display = serializers.CharField(source="get_framework_display", read_only=True)
    database_size_display = serializers.SerializerMethodField()
    site_url = serializers.SerializerMethodField()
    postgres_connection_string = serializers.SerializerMethodField()
    incidents = serializers.SerializerMethodField()
    open_incidents_count = serializers.SerializerMethodField()
    provisioning_step_display = serializers.CharField(source="get_provisioning_step_display", read_only=True)

    class Meta:
        model = Server
        fields = (
            "id",
            "name",
            "image",
            "image_display",
            "framework",
            "framework_display",
            "database_size_mb",
            "database_size_display",
            "custom_image",
            "template",
            "template_name",
            "template_cpu_limit",
            "template_memory_mb",
            "effective_cpu_limit",
            "effective_memory_mb",
            "host_port",
            "env_vars",
            "container_id",
            "status",
            "status_display",
            "provisioning_step",
            "provisioning_step_display",
            "provisioning_error",
            "created_at",
            "site_url",
            "postgres_connection_string",
            "incidents",
            "open_incidents_count",
        )
        read_only_fields = ("host_port", "container_id", "status", "created_at", "provisioning_step", "provisioning_error")

    def validate_template(self, value):
        if not value:
            raise serializers.ValidationError("A server template is required.")
        return value

    def get_site_url(self, obj):
        if obj.image not in {Server.Image.NGINX, Server.Image.PYTHON, Server.Image.NODE, Server.Image.PHP} or not obj.host_port:
            return None
        return f"http://127.0.0.1:{obj.host_port}"

    def get_postgres_connection_string(self, obj):
        return postgres_connection_string(obj)

    def get_database_size_display(self, obj):
        if obj.image != Server.Image.POSTGRES:
            return None
        return f"{obj.database_size_mb} MB" if obj.database_size_mb else "No limit"

    def get_incidents(self, obj):
        if not obj.pk:
            return []
        incidents = obj.incidents.all()[:8]
        return IncidentSerializer(incidents, many=True).data

    def get_open_incidents_count(self, obj):
        if not obj.pk:
            return 0
        return obj.incidents.filter(status="open").count()

    def validate(self, attrs):
        image = attrs.get("image", getattr(self.instance, "image", None))
        custom_image = attrs.get("custom_image", getattr(self.instance, "custom_image", ""))
        if image == Server.Image.CUSTOM and not custom_image:
            raise serializers.ValidationError({"custom_image": "A Docker image is required for custom servers."})
        if image == Server.Image.POSTGRES and not (attrs.get("env_vars", getattr(self.instance, "env_vars", {})) or {}).get(
            "POSTGRES_PASSWORD"
        ):
            raise serializers.ValidationError({"env_vars": "PostgreSQL servers require POSTGRES_PASSWORD."})
        database_size_mb = attrs.get("database_size_mb", getattr(self.instance, "database_size_mb", None))
        if database_size_mb is not None and image != Server.Image.POSTGRES:
            raise serializers.ValidationError({"database_size_mb": "Database storage limits are only available for PostgreSQL servers."})
        framework = attrs.get("framework", getattr(self.instance, "framework", ""))
        if not Server.supports_framework(image, framework):
            raise serializers.ValidationError({"framework": "Choose a framework that matches the selected image."})
        return attrs


class MetricSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = MetricSnapshot
        fields = ("timestamp", "cpu_percent", "memory_mb", "net_rx", "net_tx")


class IncidentSerializer(serializers.ModelSerializer):
    type_display = serializers.CharField(source="get_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Incident
        fields = ("id", "type", "type_display", "status", "status_display", "opened_at", "resolved_at")


class ServerTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = ServerTemplate
        fields = ("id", "name", "cpu_limit", "memory_mb", "description")