from django.db import migrations


def release_deleted_server_ports(apps, schema_editor):
    Server = apps.get_model("servers", "Server")
    Server.objects.filter(status="deleted").update(host_port=None)


class Migration(migrations.Migration):
    dependencies = [
        ("servers", "0002_server_custom_image"),
    ]

    operations = [
        migrations.RunPython(release_deleted_server_ports, migrations.RunPython.noop),
    ]