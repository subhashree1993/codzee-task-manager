from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


LEGACY_USERNAME = "legacy-task-owner"


def assign_legacy_owner(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    database = schema_editor.connection.alias
    unowned_tasks = Task.objects.using(database).filter(owner__isnull=True)

    if not unowned_tasks.exists():
        return

    username = LEGACY_USERNAME
    suffix = 1
    while User.objects.using(database).filter(username=username).exists():
        username = f"{LEGACY_USERNAME}-{suffix}"
        suffix += 1

    legacy_owner = User.objects.using(database).create(
        username=username,
        password="!",
        is_active=False,
    )
    unowned_tasks.update(owner_id=legacy_owner.pk)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tasks", "0002_alter_task_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="owner",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.RunPython(assign_legacy_owner, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="task",
            name="owner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="tasks",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]