import django.db.models.deletion
from django.db import migrations, models


def backfill_pasted_keys(apps, schema_editor):
    """Every key typed into the old single-key field becomes a named row."""
    Server = apps.get_model("servers", "Server")
    ServerSSHKey = apps.get_model("servers", "ServerSSHKey")
    ServerSSHKey.objects.bulk_create(
        [
            ServerSSHKey(server_id=server.id, label="default", public_key=server.ssh_public_key)
            for server in Server.objects.exclude(ssh_public_key="")
        ]
    )


class Migration(migrations.Migration):

    dependencies = [
        ('servers', '0015_server_ssh_allowed_from'),
    ]

    operations = [
        migrations.CreateModel(
            name='ServerSSHKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(max_length=60)),
                ('public_key', models.TextField()),
                ('allowed_from', models.TextField(blank=True, default='')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('server', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='ssh_keys', to='servers.server')),
            ],
            options={
                'ordering': ['created_at', 'id'],
                'constraints': [models.UniqueConstraint(fields=('server', 'label'), name='unique_ssh_key_label_per_server')],
            },
        ),
        migrations.RunPython(backfill_pasted_keys, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='server',
            name='ssh_public_key',
        ),
    ]
