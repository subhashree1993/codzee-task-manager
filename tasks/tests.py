from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from .models import Task


class TaskAPITests(APITestCase):
    def setUp(self):
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
            set(response.data), {"id", "title", "description", "status", "created_at"}
        )
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

    def test_list_tasks_in_deterministic_newest_first_order(self):
        older = Task.objects.create(title="Older")
        newer = Task.objects.create(title="Newer")
        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["id"] for item in response.data], [newer.pk, older.pk])

        # Equal timestamps must still have a stable order using the task ID.
        Task.objects.update(created_at=timezone.now())
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([item["id"] for item in response.data], [newer.pk, older.pk])

    def test_retrieve_task(self):
        task = Task.objects.create(title="Read task", description="Details")

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
        task = Task.objects.create(title="Original", description="Old details")
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
        task = Task.objects.create(title="Original", description="Keep these details")
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
        task = Task.objects.create(title="Original", status="pending")
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
        task = Task.objects.create(title="Delete me")
        retained = Task.objects.create(title="Keep me")
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
        task = Task.objects.create(title="Original", status="pending")

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
        task = Task.objects.create(title="Original")
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
