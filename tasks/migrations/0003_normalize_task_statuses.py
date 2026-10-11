from django.db import migrations


VALID_STATUSES = ("pending", "in_progress", "completed")


def normalize_task_statuses(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    tasks = Task.objects.using(schema_editor.connection.alias)

    tasks.filter(status="done").update(status="completed")
    tasks.exclude(status__in=VALID_STATUSES).update(status="pending")


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0002_alter_task_status"),
    ]

    operations = [
        migrations.RunPython(
            normalize_task_statuses,
            migrations.RunPython.noop,
        ),
    ]