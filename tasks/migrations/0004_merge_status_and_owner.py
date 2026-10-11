from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0003_normalize_task_statuses"),
        ("tasks", "0003_task_owner"),
    ]

    operations = []