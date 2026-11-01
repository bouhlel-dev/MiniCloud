import base64
import binascii
import ipaddress
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q


class ServerTemplate(models.Model):
    name = models.CharField(max_length=100, unique=True)
    cpu_limit = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
    )
    memory_mb = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["cpu_limit", "memory_mb"]

    def __str__(self):
        return self.name


class Server(models.Model):
    class Image(models.TextChoices):
        NGINX = "nginx", "Nginx"
        PYTHON = "python", "Python"
        NODE = "node", "Node.js"
        PHP = "php", "PHP / XAMPP"
        POSTGRES = "postgres", "PostgreSQL"
        CUSTOM = "custom", "Custom"

    class Framework(models.TextChoices):
        FASTAPI = "fastapi", "FastAPI"
        FLASK = "flask", "Flask"
        DJANGO = "django", "Django"
        EXPRESS = "express", "Express"
        NEXTJS = "nextjs", "Next.js"

    class Status(models.TextChoices):
        CREATING = "creating", "Creating"
        RUNNING = "running", "Running"
        STOPPED = "stopped", "Stopped"
        FAILED = "failed", "Failed"
        DELETED = "deleted", "Deleted"

    class ProvisioningStep(models.TextChoices):
        ALLOCATING_PORT = "allocating_port", "Allocating port"
        PULLING_IMAGE = "pulling_image", "Pulling Docker image"
        CREATING_CONTAINER = "creating_container", "Creating container"
        DOWNLOADING_XAMPP = "downloading_xampp", "Downloading XAMPP installer"
        INSTALLING_XAMPP = "installing_xampp", "Installing XAMPP"
        STARTING_APACHE = "starting_apache", "Starting Apache & verifying"
        INSTALLING_DEPENDENCIES = "installing_dependencies", "Installing dependencies"
        CONFIGURING = "configuring", "Configuring server"
        READY = "ready", "Ready"
        FAILED = "failed", "Failed"

    VALID_TRANSITIONS = {
        Status.CREATING: {Status.RUNNING, Status.FAILED, Status.DELETED},
        Status.RUNNING: {Status.STOPPED, Status.FAILED, Status.DELETED},
        Status.STOPPED: {Status.RUNNING, Status.FAILED, Status.DELETED},
        Status.FAILED: {Status.CREATING, Status.RUNNING, Status.DELETED},
        Status.DELETED: set(),
    }

    FRAMEWORKS_BY_IMAGE = {
        Image.PYTHON: {Framework.FASTAPI, Framework.FLASK, Framework.DJANGO},
        Image.NODE: {Framework.EXPRESS, Framework.NEXTJS},
    }
    DATABASE_SIZE_CHOICES = [
        (512, "512 MB"),
        (1024, "1 GB"),
        (5120, "5 GB"),
        (10240, "10 GB"),
    ]
    SSH_KEY_ALGORITHMS = {
        "ssh-rsa",
        "ssh-dss",
        "ssh-ed25519",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
    SSH_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal")
    # ipaddress.is_private cannot be trusted here: it counts the reserved
    # documentation ranges as private and misses the CGNAT block Tailscale uses.
    SSH_LOCAL_NETWORKS = tuple(
        ipaddress.ip_network(cidr)
        for cidr in (
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "100.64.0.0/10",
            "169.254.0.0/16",
            "fc00::/7",
            "fe80::/10",
        )
    )

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="servers")
    name = models.CharField(max_length=100)
    image = models.CharField(max_length=20, choices=Image.choices)
    framework = models.CharField(max_length=20, choices=Framework.choices, blank=True, default="")
    database_size_mb = models.PositiveIntegerField(blank=True, null=True, choices=DATABASE_SIZE_CHOICES)
    custom_image = models.CharField(max_length=255, blank=True)
    template = models.ForeignKey(ServerTemplate, on_delete=models.PROTECT, related_name="servers")
    host_port = models.PositiveIntegerField(
        blank=True,
        null=True,
        unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    ssh_enabled = models.BooleanField(default=False)
    ssh_port = models.PositiveIntegerField(
        blank=True,
        null=True,
        unique=True,
        validators=[MinValueValidator(1), MaxValueValidator(65535)],
    )
    ssh_public_key = models.TextField(blank=True, default="")
    ssh_allowed_from = models.TextField(blank=True, default="")
    env_vars = models.JSONField(default=dict, blank=True)
    container_id = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.CREATING)
    provisioning_step = models.CharField(max_length=30, choices=ProvisioningStep.choices, default=ProvisioningStep.ALLOCATING_PORT, blank=True)
    provisioning_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["owner", "name"],
                condition=~Q(status="deleted"),
                name="unique_active_server_name_per_owner",
            ),
        ]
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["created_at"]),
        ]

    def __str__(self):
        return f"{self.owner.username}/{self.name}"

    @classmethod
    def effective_memory_mb_for(cls, image, template):
        """Memory the container actually gets; PHP/XAMPP needs more than the template may offer."""
        memory_mb = int(template.memory_mb) if template is not None else 0
        if image == cls.Image.PHP:
            minimum = int(getattr(settings, "MINICLOUD_XAMPP_MIN_MEMORY_MB", 1024))
            return max(memory_mb, minimum)
        return memory_mb

    @property
    def effective_memory_mb(self):
        return self.effective_memory_mb_for(self.image, self.template)

    @property
    def effective_cpu_limit(self):
        return self.template.cpu_limit if self.template is not None else Decimal("0.00")

    @classmethod
    def parse_ssh_public_key(cls, value):
        """Return the normalised single-line OpenSSH public key, or raise ValidationError."""
        line = " ".join((value or "").split())
        if not line:
            raise ValidationError("Enter an OpenSSH public key to enable SSH access.")
        if len(line) > 2000:
            raise ValidationError("The SSH public key is too long.")
        parts = line.split(" ")
        algorithm = parts[0]
        if len(parts) < 2 or algorithm not in cls.SSH_KEY_ALGORITHMS:
            raise ValidationError(
                "SSH keys must be one OpenSSH public-key line starting with the algorithm, for example ssh-ed25519 AAAA…."
            )
        try:
            blob = base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValidationError("The SSH public key data is not valid base64.") from error
        name_length = int.from_bytes(blob[:4], "big") if len(blob) >= 4 else -1
        if name_length != len(algorithm) or blob[4 : 4 + name_length] != algorithm.encode("ascii"):
            raise ValidationError("The SSH public key does not match the algorithm it starts with.")
        return line

    @classmethod
    def parse_ssh_allowed_from(cls, value):
        """Normalise a comma-separated IP/CIDR allowlist; blank means any source is accepted."""
        entries = (value or "").replace("\n", ",").split(",")
        networks = list(dict.fromkeys(entry.strip() for entry in entries if entry.strip()))
        normalised = []
        for entry in networks:
            try:
                normalised.append(str(ipaddress.ip_network(entry, strict=False)))
            except ValueError as error:
                raise ValidationError(
                    f"{entry} is not an IP address or CIDR range. Hostnames are rejected because sshd "
                    "would match them through a spoofable reverse DNS lookup."
                ) from error
        return ",".join(normalised)

    @property
    def ssh_host(self):
        return settings.MINICLOUD_SSH_HOST

    @property
    def ssh_exposed(self):
        """True unless the advertised host is loopback or inside a local-use range."""
        host = (self.ssh_host or "").strip().rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(self.SSH_LOCAL_SUFFIXES):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # A static or DDNS name is treated as internet-facing; that is the point of setting one.
            return True
        return not (address.is_loopback or address.is_link_local or any(address in network for network in self.SSH_LOCAL_NETWORKS))

    @property
    def ssh_command(self):
        if not self.ssh_enabled or not self.ssh_port:
            return None
        return f"ssh -p {self.ssh_port} root@{self.ssh_host}"

    @classmethod
    def is_valid_transition(cls, current_status, new_status):
        current_status = cls.Status(current_status)
        new_status = cls.Status(new_status)
        return new_status in cls.VALID_TRANSITIONS[current_status]

    def can_transition_to(self, new_status):
        return self.is_valid_transition(self.status, new_status)

    @classmethod
    def supports_framework(cls, image, framework):
        if not framework:
            return True
        return framework in cls.FRAMEWORKS_BY_IMAGE.get(image, set())

    def transition_to(self, new_status):
        new_status = self.Status(new_status)
        if not self.pk:
            raise ValidationError({"status": "Save the server before changing its lifecycle state."})
        if new_status == self.Status(self.status):
            return
        with transaction.atomic():
            persisted = type(self).objects.select_for_update().get(pk=self.pk)
            if not self.is_valid_transition(persisted.status, new_status):
                raise ValidationError({"status": f"Cannot transition from {persisted.status} to {new_status}."})
            persisted.status = new_status
            persisted.save(update_fields=["status"])
            self.status = new_status

    def mark_deleted(self):
        self.transition_to(self.Status.DELETED)

    def clean(self):
        errors = {}
        if self.image == self.Image.CUSTOM and not self.custom_image:
            errors["custom_image"] = "A Docker image is required for custom servers."
        if not self.supports_framework(self.image, self.framework):
            errors["framework"] = "Choose a framework that is compatible with the selected image."
        if self.database_size_mb is not None and self.image != self.Image.POSTGRES:
            errors["database_size_mb"] = "Database storage limits are only available for PostgreSQL servers."
        if self.ssh_enabled:
            try:
                self.parse_ssh_public_key(self.ssh_public_key)
            except ValidationError as error:
                errors["ssh_public_key"] = error.messages[0]
            if not self.ssh_port and self.pk is not None:
                # The create forms validate before provisioning allocates the port, so only a saved row is inconsistent.
                errors["ssh_port"] = "An SSH host port is required when SSH access is enabled."
            try:
                self.parse_ssh_allowed_from(self.ssh_allowed_from)
            except ValidationError as error:
                errors["ssh_allowed_from"] = error.messages[0]
        elif self.ssh_port:
            errors["ssh_port"] = "SSH access is disabled, so no SSH host port can be published."
        if self.ssh_port and self.ssh_port == self.host_port:
            errors["ssh_port"] = "The SSH port must differ from the port published for the application."
        if not isinstance(self.env_vars, dict):
            errors["env_vars"] = "Environment variables must be a JSON object."
        elif not all(isinstance(key, str) for key in self.env_vars):
            errors["env_vars"] = "Environment variable names must be strings."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self._state.adding:
            if self.status != self.Status.CREATING:
                raise ValidationError({"status": "New servers must begin in the Creating state."})
        else:
            current_status = type(self).objects.filter(pk=self.pk).values_list("status", flat=True).first()
            if current_status and current_status != self.status and not self.is_valid_transition(current_status, self.status):
                raise ValidationError({"status": f"Cannot transition from {current_status} to {self.status}."})
        super().save(*args, **kwargs)
