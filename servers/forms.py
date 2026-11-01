import re
import secrets

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import MinLengthValidator

from .models import Server, ServerTemplate


class ServerProvisionForm(forms.ModelForm):
    postgres_database = forms.CharField(required=False, initial="minicloud", max_length=63)
    postgres_username = forms.CharField(required=False, initial="minicloud", max_length=63)
    postgres_password = forms.CharField(
        required=False,
        validators=[MinLengthValidator(12)],
        widget=forms.PasswordInput(render_value=True),
        help_text="Leave blank to generate a secure password automatically.",
    )
    ssh_public_key = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "ssh-ed25519 AAAA… user@laptop"}),
        help_text="One OpenSSH public key line. SSH is key-only: passwords stay disabled.",
    )
    ssh_allowed_from = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "203.0.113.7, 198.51.100.0/24"}),
        help_text="Source addresses allowed to connect. Leave blank to accept any source.",
    )

    class Meta:
        model = Server
        fields = ("name", "image", "framework", "database_size_mb", "custom_image", "template", "env_vars", "ssh_enabled", "ssh_public_key", "ssh_allowed_from")
        widgets = {
            "env_vars": forms.Textarea(attrs={"rows": 6, "placeholder": '{"KEY": "value"}'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["framework"].choices = [("", "Blank runtime (no framework)"), *Server.Framework.choices]
        self.fields["database_size_mb"].choices = [("", "No limit"), *Server.DATABASE_SIZE_CHOICES]

    def clean_ssh_public_key(self):
        value = self.cleaned_data.get("ssh_public_key") or ""
        if not value.strip():
            return value
        try:
            return Server.parse_ssh_public_key(value)
        except ValidationError as error:
            raise forms.ValidationError(error.messages[0]) from error

    def clean_ssh_allowed_from(self):
        value = self.cleaned_data.get("ssh_allowed_from") or ""
        if not value.strip():
            return value
        try:
            return Server.parse_ssh_allowed_from(value)
        except ValidationError as error:
            raise forms.ValidationError(error.messages[0]) from error

    def clean(self):
        cleaned_data = super().clean()
        if cleaned_data.get("image") == Server.Image.POSTGRES:
            database = cleaned_data.get("postgres_database") or "minicloud"
            username = cleaned_data.get("postgres_username") or "minicloud"
            password = cleaned_data.get("postgres_password") or secrets.token_urlsafe(24)
            if not _is_postgres_identifier(database):
                self.add_error("postgres_database", "Use letters, numbers, and underscores; start with a letter or underscore.")
            if not _is_postgres_identifier(username):
                self.add_error("postgres_username", "Use letters, numbers, and underscores; start with a letter or underscore.")
            if not self.errors:
                environment = dict(cleaned_data.get("env_vars") or {})
                environment.update(
                    {
                        "POSTGRES_DB": database,
                        "POSTGRES_USER": username,
                        "POSTGRES_PASSWORD": password,
                    }
                )
                cleaned_data["env_vars"] = environment
        if cleaned_data.get("ssh_enabled") and not (cleaned_data.get("ssh_public_key") or "").strip():
            self.add_error("ssh_public_key", "Enter an OpenSSH public key to enable SSH access.")
        return cleaned_data

    def provisioning_data(self):
        return {
            field_name: self.cleaned_data[field_name]
            for field_name in (
                "name",
                "image",
                "framework",
                "database_size_mb",
                "custom_image",
                "template",
                "env_vars",
                "ssh_enabled",
                "ssh_public_key",
                "ssh_allowed_from",
            )
        }


class ServerUpdateForm(forms.ModelForm):
    class Meta:
        model = Server
        fields = ("name",)


class ServerTemplateForm(forms.ModelForm):
    class Meta:
        model = ServerTemplate
        fields = ("name", "cpu_limit", "memory_mb", "description")
        widgets = {"description": forms.Textarea(attrs={"rows": 4})}


def _is_postgres_identifier(value):
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value))
