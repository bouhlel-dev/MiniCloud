import os
import secrets

from django.core.management.base import BaseCommand

from accounts.models import User
from servers.models import ServerTemplate


class Command(BaseCommand):
    help = "Creates the built-in server templates and a demo user."

    templates = (
        {"name": "Small", "cpu_limit": "0.50", "memory_mb": 256, "description": "0.5 CPU and 256 MB memory."},
        {"name": "Medium", "cpu_limit": "1.00", "memory_mb": 512, "description": "1 CPU and 512 MB memory."},
        {"name": "Large", "cpu_limit": "2.00", "memory_mb": 1024, "description": "2 CPU and 1024 MB memory."},
    )

    def add_arguments(self, parser):
        parser.add_argument("--username", default="demo")
        parser.add_argument("--email", default="demo@example.com")
        parser.add_argument("--password", default=os.getenv("DEMO_USER_PASSWORD"))

    def handle(self, *args, **options):
        for template in self.templates:
            ServerTemplate.objects.update_or_create(name=template["name"], defaults=template)
        self.stdout.write(self.style.SUCCESS("Server templates are ready."))

        password = options["password"] or secrets.token_urlsafe(18)
        user, created = User.objects.get_or_create(
            username=options["username"],
            defaults={"email": options["email"]},
        )
        if created:
            user.set_password(password)
            user.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS(f"Created demo user '{user.username}'."))
            if not options["password"]:
                self.stdout.write(self.style.WARNING(f"Generated demo password: {password}"))
        else:
            self.stdout.write(f"Demo user '{user.username}' already exists.")

