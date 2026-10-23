from django.contrib import admin

from .models import HealthCheck, Incident, MetricSnapshot


@admin.register(MetricSnapshot)
class MetricSnapshotAdmin(admin.ModelAdmin):
    list_display = ("server", "cpu_percent", "memory_mb", "timestamp")
    list_filter = ("server",)


@admin.register(HealthCheck)
class HealthCheckAdmin(admin.ModelAdmin):
    list_display = ("server", "auto_restart_attempts", "last_checked_at", "last_container_status")


@admin.register(Incident)
class IncidentAdmin(admin.ModelAdmin):
    list_display = ("server", "type", "status", "opened_at", "resolved_at")
    list_filter = ("type", "status")

