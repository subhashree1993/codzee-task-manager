import base64

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .models import Task


class TaskAPITests(APITestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="task-owner", password="test-password"
        )
        self.client.force_authenticate(user=self.user)
        self.list_url = reverse("task-list")

    def detail_url(self, task):
        return reverse("task-detail", args=[task.pk])

    def test_list_empty(self):
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_create_task(self):
        payload = {
            "title": "Prepare interview",
            "description": "Review the assignment requirements",
            "status": "in_progress",
        }

        response = self.client.post(self.list_url, payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Task.objects.count(), 1)
        task = Task.objects.get(pk=response.data["id"])
        for field, value in payload.items():
            self.assertEqual(getattr(task, field), value)
            self.assertEqual(response.data[field], value)
        self.assertEqual(
            set(response.data),
            {"id", "title", "description", "status", "created_at", "owner"},
        )
        self.assertEqual(response.data["owner"], self.user.pk)
        self.assertIsNotNone(task.created_at)
        self.assertIsNotNone(response.data["created_at"])

    def test_create_with_defaults(self):
        response = self.client.post(
            self.list_url, {"title": "Write README"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        task = Task.objects.get(pk=response.data["id"])
        self.assertEqual(task.description, "")
        self.assertEqual(task.status, "pending")
        self.assertEqual(response.data["description"], "")
        self.assertEqual(response.data["status"], "pending")

    def test_create_rejects_invalid_status(self):
        response = self.client.post(
            self.list_url,
            {"title": "Invalid status", "status": "blocked"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertEqual(Task.objects.count(), 0)

    def test_create_rejects_blank_and_whitespace_titles(self):
        for title in ("", " \t\n"):
            with self.subTest(title=repr(title)):
                response = self.client.post(
                    self.list_url, {"title": title}, format="json"
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("title", response.data)
        self.assertEqual(Task.objects.count(), 0)

    def test_title_length_limit(self):
        accepted_title = "T" * 200
        accepted_response = self.client.post(
            self.list_url, {"title": accepted_title}, format="json"
        )

        self.assertEqual(accepted_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(accepted_response.data["title"], accepted_title)

        rejected_response = self.client.post(
            self.list_url, {"title": "T" * 201}, format="json"
        )

        self.assertEqual(rejected_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", rejected_response.data)
        self.assertEqual(Task.objects.count(), 1)

    def test_create_trims_title_whitespace(self):
        response = self.client.post(
            self.list_url, {"title": "  Prepare interview  "}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["title"], "Prepare interview")

    def test_anonymous_requests_are_rejected(self):
        task = Task.objects.create(title="Private task", owner=self.user)
        self.client.force_authenticate(user=None)

        responses = (
            self.client.get(self.list_url),
            self.client.post(
                self.list_url, {"title": "Anonymous task"}, format="json"
            ),
            self.client.get(self.detail_url(task)),
            self.client.put(
                self.detail_url(task), {"title": "Changed"}, format="json"
            ),
            self.client.patch(
                self.detail_url(task), {"title": "Changed"}, format="json"
            ),
            self.client.delete(self.detail_url(task)),
        )

        for response in responses:
            with self.subTest(status_code=response.status_code):
                self.assertEqual(
                    response.status_code, status.HTTP_401_UNAUTHORIZED
                )
        task.refresh_from_db()
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(task.title, "Private task")

    def test_create_uses_authenticated_owner_and_ignores_submitted_owner(self):
        other_user = get_user_model().objects.create_user(
            username="other-owner", password="test-password"
        )

        response = self.client.post(
            self.list_url,
            {"title": "Owned by requester", "owner": other_user.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        task = Task.objects.get(pk=response.data["id"])
        self.assertEqual(task.owner, self.user)
        self.assertEqual(response.data["owner"], self.user.pk)

    def test_owner_is_read_only_on_update(self):
        other_user = get_user_model().objects.create_user(
            username="other-owner", password="test-password"
        )
        task = Task.objects.create(title="Original", owner=self.user)

        response = self.client.patch(
            self.detail_url(task), {"owner": other_user.pk}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.owner, self.user)
        self.assertEqual(response.data["owner"], self.user.pk)

    def test_users_can_only_access_their_own_tasks(self):
        other_user = get_user_model().objects.create_user(
            username="other-owner", password="test-password"
        )
        own_task = Task.objects.create(title="Mine", owner=self.user)
        other_task = Task.objects.create(title="Theirs", owner=other_user)

        list_response = self.client.get(self.list_url)
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["id"] for item in list_response.data], [own_task.pk])

        for method in ("get", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    self.detail_url(other_task),
                    {"title": "Attempted access"},
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        other_task.refresh_from_db()
        self.assertEqual(other_task.title, "Theirs")
        self.assertEqual(other_task.owner, other_user)

    def test_list_tasks_in_deterministic_newest_first_order(self):
        older = Task.objects.create(title="Older", owner=self.user)
        newer = Task.objects.create(title="Newer", owner=self.user)
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["id"] for item in response.data], [newer.pk, older.pk])

        # Equal timestamps must still have a stable order using the task ID.
        Task.objects.update(created_at=timezone.now())
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["id"] for item in response.data], [newer.pk, older.pk])

    def test_retrieve_task(self):
        task = Task.objects.create(
            title="Read task", description="Details", owner=self.user
        )

        response = self.client.get(self.detail_url(task))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], task.pk)
        self.assertEqual(response.data["title"], task.title)
        self.assertEqual(response.data["description"], task.description)
        self.assertEqual(response.data["status"], task.status)

    def test_missing_task_returns_404(self):
        url = reverse("task-detail", args=[99999])
        for method in ("get", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(
                    url, {"title": "Missing"}, format="json"
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Task.objects.count(), 0)

    def test_put_updates_task(self):
        task = Task.objects.create(
            title="Original", description="Old details", owner=self.user
        )
        original_created_at = task.created_at
        payload = {
            "title": "Updated",
            "description": "New details",
            "status": "completed",
        }

        response = self.client.put(self.detail_url(task), payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        for field, value in payload.items():
            self.assertEqual(getattr(task, field), value)
            self.assertEqual(response.data[field], value)
        self.assertEqual(task.created_at, original_created_at)

    def test_patch_preserves_unspecified_fields(self):
        task = Task.objects.create(
            title="Original", description="Keep these details", owner=self.user
        )
        original_created_at = task.created_at

        response = self.client.patch(
            self.detail_url(task), {"status": "completed"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.title, "Original")
        self.assertEqual(task.description, "Keep these details")
        self.assertEqual(task.created_at, original_created_at)

    def test_patch_rejects_invalid_values_without_modifying_task(self):
        task = Task.objects.create(title="Original", status="pending", owner=self.user)
        invalid_payloads = (
            {"status": "blocked"},
            {"title": " \t\n"},
            {"title": "T" * 201},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.patch(
                    self.detail_url(task), payload, format="json"
                )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(next(iter(payload)), response.data)
                task.refresh_from_db()
                self.assertEqual(task.title, "Original")
                self.assertEqual(task.status, "pending")

    def test_delete_task(self):
        task = Task.objects.create(title="Delete me", owner=self.user)
        retained = Task.objects.create(title="Keep me", owner=self.user)
        url = self.detail_url(task)

        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response.content, b"")
        self.assertFalse(Task.objects.filter(pk=task.pk).exists())
        self.assertTrue(Task.objects.filter(pk=retained.pk).exists())
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_create_requires_title(self):
        response = self.client.post(
            self.list_url, {"description": "No title"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", response.data)
        self.assertEqual(Task.objects.count(), 0)

    def test_put_requires_title_and_does_not_modify_task_on_error(self):
        task = Task.objects.create(title="Original", status="pending", owner=self.user)

        response = self.client.put(
            self.detail_url(task), {"status": "completed"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("title", response.data)
        task.refresh_from_db()
        self.assertEqual(task.title, "Original")
        self.assertEqual(task.status, "pending")

    def test_client_cannot_set_id_or_created_at_on_create(self):
        response = self.client.post(
            self.list_url,
            {
                "title": "Server-managed fields",
                "id": 99999,
                "created_at": "2000-01-01T00:00:00Z",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        task = Task.objects.get(pk=response.data["id"])
        self.assertNotEqual(task.pk, 99999)
        self.assertNotEqual(task.created_at.year, 2000)

    def test_client_cannot_change_id_or_created_at_on_update(self):
        task = Task.objects.create(title="Original", owner=self.user)
        original_pk = task.pk
        original_created_at = task.created_at

        response = self.client.patch(
            self.detail_url(task),
            {"title": "Updated", "id": 99999, "created_at": "2000-01-01T00:00:00Z"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        task.refresh_from_db()
        self.assertEqual(task.pk, original_pk)
        self.assertEqual(task.created_at, original_created_at)
        self.assertEqual(task.title, "Updated")
        self.assertEqual(response.data["id"], original_pk)
        self.assertEqual(Task.objects.count(), 1)


class TaskOwnershipMigrationTests(TransactionTestCase):
    def tearDown(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDown()

    def migrate_from_pre_ownership(self):
        migrate_from = [("tasks", "0002_alter_task_status")]
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        other_apps = [
            node
            for node in executor.loader.graph.leaf_nodes()
            if node[0] != "tasks"
        ]
        return executor.loader.project_state(other_apps + migrate_from).apps

    def migrate_to_ownership(self):
        migrate_to = [("tasks", "0003_task_owner")]
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        other_apps = [
            node
            for node in executor.loader.graph.leaf_nodes()
            if node[0] != "tasks"
        ]
        return executor.loader.project_state(other_apps + migrate_to).apps

    def test_no_username_collision_uses_exact_legacy_username(self):
        old_apps = self.migrate_from_pre_ownership()
        old_task_model = old_apps.get_model("tasks", "Task")
        legacy_task = old_task_model.objects.create(title="Pre-ownership task")

        new_apps = self.migrate_to_ownership()
        new_task_model = new_apps.get_model("tasks", "Task")
        user_model = new_apps.get_model("auth", "User")

        migrated_task = new_task_model.objects.get(pk=legacy_task.pk)
        legacy_owner = user_model.objects.get(pk=migrated_task.owner_id)
        self.assertEqual(legacy_owner.username, "legacy-task-owner")
        self.assertEqual(legacy_owner.email, "legacy-task-owner@codzee.invalid")
        self.assertFalse(legacy_owner.is_active)
        self.assertEqual(legacy_owner.password, "!")
        self.assertEqual(migrated_task.title, "Pre-ownership task")

    def test_username_collision_uses_suffix_and_preserves_existing_user(self):
        old_apps = self.migrate_from_pre_ownership()
        old_task_model = old_apps.get_model("tasks", "Task")
        old_user_model = old_apps.get_model("auth", "User")
        existing_user = old_user_model.objects.create(
            username="legacy-task-owner",
            email="real-user@example.com",
            password="existing-password-hash",
            is_active=True,
        )
        legacy_task = old_task_model.objects.create(
            title="Existing owner's task",
            description="Must remain unchanged",
            status="in_progress",
        )

        new_apps = self.migrate_to_ownership()
        new_task_model = new_apps.get_model("tasks", "Task")
        user_model = new_apps.get_model("auth", "User")

        preserved_user = user_model.objects.get(pk=existing_user.pk)
        self.assertEqual(preserved_user.username, "legacy-task-owner")
        self.assertEqual(preserved_user.email, "real-user@example.com")
        self.assertEqual(preserved_user.password, "existing-password-hash")
        self.assertTrue(preserved_user.is_active)

        migrated_task = new_task_model.objects.get(pk=legacy_task.pk)
        generated_owner = user_model.objects.get(pk=migrated_task.owner_id)
        self.assertEqual(generated_owner.username, "legacy-task-owner-1")
        self.assertEqual(generated_owner.email, "legacy-task-owner@codzee.invalid")
        self.assertFalse(generated_owner.is_active)
        self.assertEqual(generated_owner.password, "!")
        self.assertNotEqual(generated_owner.pk, existing_user.pk)
        self.assertEqual(migrated_task.title, "Existing owner's task")
        self.assertEqual(migrated_task.description, "Must remain unchanged")
        self.assertEqual(migrated_task.status, "in_progress")


class TaskBasicAuthenticationTests(APITestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = get_user_model().objects.create_user(
            username="basic-user", password="correct-password"
        )
        self.task = Task.objects.create(title="Protected task", owner=self.user)
        self.list_url = reverse("task-list")
        self.detail_url = reverse("task-detail", args=[self.task.pk])

    def basic_auth_headers(self, username, password):
        credentials = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        return {"HTTP_AUTHORIZATION": f"Basic {credentials}"}

    def assert_rejected_basic_auth_cannot_mutate_tasks(self, username, password):
        headers = self.basic_auth_headers(username, password)
        responses = (
            self.client.get(self.list_url, **headers),
            self.client.post(
                self.list_url,
                {"title": "Must not be created"},
                format="json",
                **headers,
            ),
            self.client.put(
                self.detail_url,
                {"title": "Must not be changed"},
                format="json",
                **headers,
            ),
            self.client.patch(
                self.detail_url,
                {"title": "Must not be changed"},
                format="json",
                **headers,
            ),
            self.client.delete(self.detail_url, **headers),
        )

        for response in responses:
            with self.subTest(status_code=response.status_code):
                self.assertEqual(
                    response.status_code, status.HTTP_401_UNAUTHORIZED
                )

        self.task.refresh_from_db()
        self.assertEqual(Task.objects.count(), 1)
        self.assertEqual(self.task.title, "Protected task")
        self.assertEqual(self.task.owner, self.user)

    def test_valid_basic_credentials_are_accepted(self):
        response = self.client.get(
            self.list_url,
            **self.basic_auth_headers("basic-user", "correct-password"),
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_incorrect_password_returns_401_without_mutations(self):
        self.assert_rejected_basic_auth_cannot_mutate_tasks(
            "basic-user", "incorrect-password"
        )

    def test_inactive_user_returns_401_without_mutations(self):
        inactive_user = get_user_model().objects.create_user(
            username="inactive-user",
            password="valid-password",
            is_active=False,
        )
        self.assertTrue(inactive_user.has_usable_password())

        self.assert_rejected_basic_auth_cannot_mutate_tasks(
            "inactive-user", "valid-password"
        )

    def test_unusable_password_user_returns_401_without_mutations(self):
        legacy_user = get_user_model().objects.create(
            username="legacy-task-owner",
            email="legacy-task-owner@codzee.invalid",
            password="!",
            is_active=False,
        )
        self.assertFalse(legacy_user.has_usable_password())

        self.assert_rejected_basic_auth_cannot_mutate_tasks(
            "legacy-task-owner", "!"
        )
