from decimal import Decimal

from django.apps import apps
from django.contrib.auth.models import AbstractUser
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce


class User(AbstractUser):
    max_servers = models.PositiveIntegerField(default=3, validators=[MinValueValidator(0)])
    max_cpu_cores = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("4.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
    )
    max_memory_mb = models.PositiveIntegerField(default=4096, validators=[MinValueValidator(0)])

    def quota_usage(self):
        if not self.pk:
            return {"servers": 0, "cpu_cores": Decimal("0.00"), "memory_mb": 0}

        server_model = apps.get_model("servers", "Server")
        active_servers = server_model.objects.filter(owner_id=self.pk).exclude(status=server_model.Status.DELETED)
        servers = list(active_servers.select_related("template"))
        usage = active_servers.aggregate(
            cpu_cores=Coalesce(
                Sum("template__cpu_limit"),
                Value(Decimal("0.00"), output_field=DecimalField(max_digits=6, decimal_places=2)),
            ),
        )
        return {
            "servers": len(servers),
            "cpu_cores": usage["cpu_cores"],
            # Count what the containers actually reserve (PHP/XAMPP gets a floor).
            "memory_mb": sum(server.effective_memory_mb for server in servers),
        }

    def has_capacity_for(self, template, image=None):
        usage = self.quota_usage()
        requested_cpu = Decimal(str(template.cpu_limit))
        server_model = apps.get_model("servers", "Server")
        requested_memory = server_model.effective_memory_mb_for(image, template)
        return (
            usage["servers"] < self.max_servers
            and usage["cpu_cores"] + requested_cpu <= Decimal(self.max_cpu_cores)
            and usage["memory_mb"] + requested_memory <= self.max_memory_mb
        )
