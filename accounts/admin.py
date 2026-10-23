from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class MiniCloudUserAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Provisioning quotas", {"fields": ("max_servers", "max_cpu_cores", "max_memory_mb")} ),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Provisioning quotas", {"fields": ("max_servers", "max_cpu_cores", "max_memory_mb")} ),
    )
    list_display = UserAdmin.list_display + ("max_servers", "max_cpu_cores", "max_memory_mb")

