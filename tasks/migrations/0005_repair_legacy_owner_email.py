import re

from django.conf import settings
from django.contrib.auth.hashers import is_password_usable
from django.db import migrations


LEGACY_EMAIL = "legacy-task-owner@codzee.invalid"
LEGACY_USERNAME = re.compile(r"legacy-task-owner(?:-[0-9]+)?")


def repair_legacy_owner_email(apps, schema_editor):
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    Task = apps.get_model("tasks", "Task")
    database = schema_editor.connection.alias

    if User.objects.using(database).filter(email=LEGACY_EMAIL).exists():
        return

    eligible_ids = []

    candidates = User.objects.using(database).filter(
        is_active=False,
        password__startswith="!",
    )
    for candidate in candidates.iterator():
        if LEGACY_USERNAME.fullmatch(candidate.username) is None:
            continue
        if (candidate.email or "").strip():
            continue
        if is_password_usable(candidate.password):
            continue
        if not Task.objects.using(database).filter(owner_id=candidate.pk).exists():
            continue

        eligible_ids.append(candidate.pk)
        if len(eligible_ids) > 1:
            return

    if len(eligible_ids) == 1:
        User.objects.using(database).filter(pk=eligible_ids[0]).update(
            email=LEGACY_EMAIL
        )


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("tasks", "0004_merge_status_and_owner"),
    ]

    operations = [
        migrations.RunPython(
            repair_legacy_owner_email,
            migrations.RunPython.noop,
        ),
    ]