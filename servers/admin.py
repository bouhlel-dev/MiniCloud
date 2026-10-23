from django.contrib import admin

from .models import Server, ServerTemplate


@admin.register(ServerTemplate)
class ServerTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "cpu_limit", "memory_mb")
    search_fields = ("name",)


@admin.register(Server)
class ServerAdmin(admin.ModelAdmin):
    list_display = ("name", "owner", "image", "status", "host_port", "created_at")
    list_filter = ("status", "image")
    search_fields = ("name", "owner__username", "container_id")
    readonly_fields = ("container_id", "created_at")
