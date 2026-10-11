import base64
from urllib.parse import parse_qs, urlparse

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .models import Task


class TaskStatusMigrationTests(TransactionTestCase):
    def tearDown(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDown()

    def test_legacy_statuses_are_normalized_without_changing_task_data(self):
        migrate_from = [("tasks", "0002_alter_task_status")]
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_from)
        task_model = executor.loader.project_state(migrate_from).apps.get_model(
            "tasks", "Task"
        )

        original_tasks = {}
        for status_value in (
            "pending",
            "in_progress",
            "completed",
            "done",
            "blocked",
        ):
            task = task_model.objects.create(
                title=f"Task with {status_value} status",
                description=f"Preserve details for {status_value}",
                status=status_value,
            )
            original_tasks[task.pk] = (
                task.title,
                task.description,
                status_value,
            )

        migrate_to = [("tasks", "0003_normalize_task_statuses")]
        executor = MigrationExecutor(connection)
        executor.migrate(migrate_to)
        migrated_model = executor.loader.project_state(migrate_to).apps.get_model(
            "tasks", "Task"
        )
        expected_statuses = {
            "pending": "pending",
            "in_progress": "in_progress",
            "completed": "completed",
            "done": "completed",
            "blocked": "pending",
        }

        for task_id, (title, description, old_status) in original_tasks.items():
            with self.subTest(old_status=old_status):
                task = migrated_model.objects.get(pk=task_id)
                self.assertEqual(task.title, title)
                self.assertEqual(task.description, description)
                self.assertEqual(task.status, expected_statuses[old_status])


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
        self.assertEqual(
            response.data,
            {"count": 0, "next": None, "previous": None, "results": []},
        )

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
        self.assertEqual(
            [item["id"] for item in list_response.data["results"]], [own_task.pk]
        )

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
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [newer.pk, older.pk],
        )

        # Equal timestamps must still have a stable order using the task ID.
        Task.objects.update(created_at=timezone.now())
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [newer.pk, older.pk],
        )

    def test_status_filter_choices_and_empty_status(self):
        tasks_by_status = {
            status_value: Task.objects.create(
                title=f"Task {status_value}", status=status_value, owner=self.user
            )
            for status_value, _label in Task.STATUS_CHOICES
        }

        for status_value, task in tasks_by_status.items():
            with self.subTest(status=status_value):
                response = self.client.get(self.list_url, {"status": status_value})
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["count"], 1)
                self.assertEqual(
                    [item["id"] for item in response.data["results"]], [task.pk]
                )

        for params in ({}, {"status": ""}):
            with self.subTest(params=params):
                response = self.client.get(self.list_url, params)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(response.data["count"], len(tasks_by_status))

    def test_unknown_status_filter_returns_clear_error(self):
        response = self.client.get(self.list_url, {"status": "blocked"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)
        self.assertIn("Choose a valid status", str(response.data["status"]))

    def test_search_matches_title_and_description_case_insensitively(self):
        title_match = Task.objects.create(
            title="Interview preparation", owner=self.user
        )
        description_match = Task.objects.create(
            title="Weekly review", description="INTERVIEW notes", owner=self.user
        )
        Task.objects.create(title="Unrelated", description="No match", owner=self.user)

        response = self.client.get(self.list_url, {"search": "  interview  "})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {item["id"] for item in response.data["results"]},
            {title_match.pk, description_match.pk},
        )

    def test_blank_search_and_no_match(self):
        first = Task.objects.create(title="First", owner=self.user)
        second = Task.objects.create(title="Second", owner=self.user)

        blank_response = self.client.get(self.list_url, {"search": " \t "})
        self.assertEqual(blank_response.status_code, status.HTTP_200_OK)
        self.assertEqual(blank_response.data["count"], 2)
        self.assertEqual(
            {item["id"] for item in blank_response.data["results"]},
            {first.pk, second.pk},
        )

        no_match_response = self.client.get(
            self.list_url, {"search": "absent-term"}
        )
        self.assertEqual(no_match_response.status_code, status.HTTP_200_OK)
        self.assertEqual(no_match_response.data["count"], 0)
        self.assertEqual(no_match_response.data["results"], [])
        self.assertIsNone(no_match_response.data["next"])
        self.assertIsNone(no_match_response.data["previous"])

    def test_status_and_search_filters_combine_with_and(self):
        expected = Task.objects.create(
            title="Interview prep", status="pending", owner=self.user
        )
        other_pending_match = Task.objects.create(
            title="Other prep",
            description="Interview notes",
            status="pending",
            owner=self.user,
        )
        Task.objects.create(
            title="Interview completed", status="completed", owner=self.user
        )
        Task.objects.create(title="Other", status="pending", owner=self.user)

        response = self.client.get(
            self.list_url, {"status": "pending", "search": "interview"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            {item["id"] for item in response.data["results"]},
            {expected.pk, other_pending_match.pk},
        )

    def test_pagination_spans_multiple_pages_in_stable_order(self):
        tasks = [
            Task.objects.create(title=f"Task {index}", owner=self.user)
            for index in range(5)
        ]
        Task.objects.filter(pk__in=[task.pk for task in tasks]).update(
            created_at=timezone.now()
        )
        expected_ids = sorted((task.pk for task in tasks), reverse=True)

        pages = [
            self.client.get(self.list_url, {"page": page, "page_size": 2})
            for page in (1, 2, 3)
        ]

        for response in pages:
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["count"], 5)
        self.assertIsNone(pages[0].data["previous"])
        self.assertIsNotNone(pages[0].data["next"])
        self.assertIsNotNone(pages[1].data["previous"])
        self.assertIsNotNone(pages[1].data["next"])
        self.assertIsNotNone(pages[2].data["previous"])
        self.assertIsNone(pages[2].data["next"])
        actual_ids = [
            item["id"]
            for response in pages
            for item in response.data["results"]
        ]
        self.assertEqual(actual_ids, expected_ids)

    def test_default_and_capped_page_sizes_and_invalid_page_size(self):
        tasks = [
            Task.objects.create(title=f"Task {index}", owner=self.user)
            for index in range(101)
        ]

        default_response = self.client.get(self.list_url)
        self.assertEqual(default_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(default_response.data["results"]), 10)
        self.assertIsNotNone(default_response.data["next"])

        capped_response = self.client.get(self.list_url, {"page_size": 500})
        self.assertEqual(capped_response.status_code, status.HTTP_200_OK)
        self.assertEqual(capped_response.data["count"], len(tasks))
        self.assertEqual(len(capped_response.data["results"]), 100)
        self.assertIsNotNone(capped_response.data["next"])

        invalid_size_response = self.client.get(
            self.list_url, {"page_size": "not-a-number"}
        )
        self.assertEqual(invalid_size_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(invalid_size_response.data["results"]), 10)

        non_positive_size_response = self.client.get(
            self.list_url, {"page_size": 0}
        )
        self.assertEqual(non_positive_size_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(non_positive_size_response.data["results"]), 10)

    def test_invalid_page_returns_not_found(self):
        Task.objects.create(title="Only task", owner=self.user)

        for page in ("not-a-number", "2"):
            with self.subTest(page=page):
                response = self.client.get(
                    self.list_url, {"page": page, "page_size": 1}
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_pagination_links_preserve_status_and_search(self):
        Task.objects.create(
            title="Interview prep one", status="pending", owner=self.user
        )
        Task.objects.create(
            title="Interview prep two", status="pending", owner=self.user
        )
        Task.objects.create(
            title="Interview done", status="completed", owner=self.user
        )

        response = self.client.get(
            self.list_url,
            {"status": "pending", "search": "interview", "page_size": 1},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        next_query = parse_qs(urlparse(response.data["next"]).query)
        self.assertEqual(next_query["status"], ["pending"])
        self.assertEqual(next_query["search"], ["interview"])
        self.assertEqual(next_query["page_size"], ["1"])
        self.assertEqual(next_query["page"], ["2"])

    def test_other_users_do_not_affect_filtered_counts_or_any_page(self):
        own_tasks = [
            Task.objects.create(
                title=f"Shared interview {index}", status="pending", owner=self.user
            )
            for index in range(3)
        ]
        other_user = get_user_model().objects.create_user(
            username="pagination-other", password="test-password"
        )
        other_tasks = [
            Task.objects.create(
                title=f"Shared interview other {index}",
                status="pending",
                owner=other_user,
            )
            for index in range(5)
        ]

        responses = [
            self.client.get(
                self.list_url,
                {
                    "status": "pending",
                    "search": "shared interview",
                    "page": page,
                    "page_size": 2,
                },
            )
            for page in (1, 2)
        ]

        own_ids = {task.pk for task in own_tasks}
        other_ids = {task.pk for task in other_tasks}
        returned_ids = set()
        for response in responses:
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["count"], 3)
            page_ids = {item["id"] for item in response.data["results"]}
            self.assertTrue(page_ids <= own_ids)
            self.assertTrue(page_ids.isdisjoint(other_ids))
            returned_ids.update(page_ids)
        self.assertEqual(returned_ids, own_ids)

    def test_list_query_parameters_do_not_change_detail_crud(self):
        task = Task.objects.create(
            title="Detail remains accessible", status="pending", owner=self.user
        )
        url = (
            f"{self.detail_url(task)}?status=unknown&search=no-match"
            "&page=invalid&page_size=0"
        )

        retrieve_response = self.client.get(url)
        self.assertEqual(retrieve_response.status_code, status.HTTP_200_OK)
        self.assertEqual(retrieve_response.data["id"], task.pk)

        put_response = self.client.put(
            url,
            {"title": "Updated with PUT", "status": "pending"},
            format="json",
        )
        self.assertEqual(put_response.status_code, status.HTTP_200_OK)

        patch_response = self.client.patch(url, {"title": "Updated with PATCH"})
        self.assertEqual(patch_response.status_code, status.HTTP_200_OK)

        delete_response = self.client.delete(url)
        self.assertEqual(delete_response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Task.objects.filter(pk=task.pk).exists())

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

        credentials = base64.b64encode(
            f"{generated_owner.username}:!".encode("utf-8")
        ).decode("ascii")
        client = APIClient()
        response = client.patch(
            reverse("task-detail", args=[migrated_task.pk]),
            {"title": "Must remain unchanged"},
            format="json",
            HTTP_AUTHORIZATION=f"Basic {credentials}",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        migrated_task.refresh_from_db()
        self.assertEqual(migrated_task.title, "Existing owner's task")


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


class TaskLegacyOwnerMarkerMigrationTests(TransactionTestCase):
    legacy_email = "legacy-task-owner@codzee.invalid"

    def tearDown(self):
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        finally:
            super().tearDown()

    def migrate_tasks_to(self, migration_name):
        target = [("tasks", migration_name)]
        executor = MigrationExecutor(connection)
        executor.migrate(target)
        other_apps = [
            node
            for node in executor.loader.graph.leaf_nodes()
            if node[0] != "tasks"
        ]
        return executor.loader.project_state(other_apps + target).apps

    def test_original_blank_email_account_is_repaired_without_other_changes(self):
        old_apps = self.migrate_tasks_to("0004_merge_status_and_owner")
        user_model = old_apps.get_model("auth", "User")
        task_model = old_apps.get_model("tasks", "Task")

        legacy_owner = user_model.objects.create(
            username="legacy-task-owner-1",
            email="",
            password="!",
            is_active=False,
        )
        legacy_task = task_model.objects.create(
            title="Original legacy task",
            description="Keep this description",
            status="completed",
            owner_id=legacy_owner.pk,
        )
        original_task = (
            legacy_task.title,
            legacy_task.description,
            legacy_task.status,
            legacy_task.owner_id,
        )
        original_tasks = {legacy_task.pk: original_task}

        unrelated_accounts = [
            user_model.objects.create(
                username="legacy-task-owner-2",
                email="",
                password="!",
                is_active=True,
            ),
            user_model.objects.create(
                username="legacy-task-owner-3",
                email="",
                password="usable-password-hash",
                is_active=False,
            ),
            user_model.objects.create(
                username="legacy-task-owner-4",
                email="person@example.com",
                password="!",
                is_active=False,
            ),
            user_model.objects.create(
                username="legacy-task-ownerish",
                email="",
                password="!",
                is_active=False,
            ),
        ]
        for account in unrelated_accounts:
            task = task_model.objects.create(
                title=f"Owned by {account.username}",
                description="Unrelated task",
                status="pending",
                owner_id=account.pk,
            )
            original_tasks[task.pk] = (
                task.title,
                task.description,
                task.status,
                task.owner_id,
            )
        original_unrelated_accounts = {
            account.pk: (account.username, account.email, account.password, account.is_active)
            for account in unrelated_accounts
        }

        new_apps = self.migrate_tasks_to("0005_repair_legacy_owner_email")
        new_user_model = new_apps.get_model("auth", "User")
        new_task_model = new_apps.get_model("tasks", "Task")

        repaired_owner = new_user_model.objects.get(pk=legacy_owner.pk)
        self.assertEqual(repaired_owner.email, self.legacy_email)
        self.assertFalse(repaired_owner.is_active)
        self.assertEqual(repaired_owner.password, "!")

        for task_id, original in original_tasks.items():
            with self.subTest(task_id=task_id):
                task = new_task_model.objects.get(pk=task_id)
                self.assertEqual(
                    (task.title, task.description, task.status, task.owner_id),
                    original,
                )

        for account_id, original in original_unrelated_accounts.items():
            with self.subTest(account_id=account_id):
                account = new_user_model.objects.get(pk=account_id)
                self.assertEqual(
                    (account.username, account.email, account.password, account.is_active),
                    original,
                )

    def test_ambiguous_legacy_accounts_are_left_unmarked(self):
        old_apps = self.migrate_tasks_to("0004_merge_status_and_owner")
        user_model = old_apps.get_model("auth", "User")
        task_model = old_apps.get_model("tasks", "Task")

        candidate_ids = []
        for username in ("legacy-task-owner", "legacy-task-owner-1"):
            candidate = user_model.objects.create(
                username=username,
                email="",
                password="!",
                is_active=False,
            )
            candidate_ids.append(candidate.pk)
            task_model.objects.create(
                title=f"Owned by {username}",
                description="Keep task data",
                status="pending",
                owner_id=candidate.pk,
            )

        new_apps = self.migrate_tasks_to("0005_repair_legacy_owner_email")
        new_user_model = new_apps.get_model("auth", "User")
        new_task_model = new_apps.get_model("tasks", "Task")

        for account_id in candidate_ids:
            with self.subTest(account_id=account_id):
                account = new_user_model.objects.get(pk=account_id)
                self.assertEqual(account.email, "")
                task = new_task_model.objects.get(owner_id=account_id)
                self.assertEqual(task.title, f"Owned by {account.username}")
                self.assertEqual(task.description, "Keep task data")

    def test_already_marked_account_is_unchanged(self):
        old_apps = self.migrate_tasks_to("0004_merge_status_and_owner")
        user_model = old_apps.get_model("auth", "User")
        task_model = old_apps.get_model("tasks", "Task")
        account = user_model.objects.create(
            username="legacy-task-owner",
            email=self.legacy_email,
            password="!",
            is_active=False,
        )
        task = task_model.objects.create(
            title="Already marked owner's task",
            description="Keep this task",
            status="pending",
            owner_id=account.pk,
        )
        another_candidate = user_model.objects.create(
            username="legacy-task-owner-1",
            email="",
            password="!",
            is_active=False,
        )
        task_model.objects.create(
            title="Another candidate's task",
            description="Leave it alone when marker exists",
            status="pending",
            owner_id=another_candidate.pk,
        )

        new_apps = self.migrate_tasks_to("0005_repair_legacy_owner_email")
        new_user_model = new_apps.get_model("auth", "User")
        new_task_model = new_apps.get_model("tasks", "Task")
        marked_account = new_user_model.objects.get(pk=account.pk)
        unchanged_task = new_task_model.objects.get(pk=task.pk)

        self.assertEqual(marked_account.email, self.legacy_email)
        self.assertFalse(marked_account.is_active)
        self.assertEqual(marked_account.password, "!")
        unchanged_candidate = new_user_model.objects.get(pk=another_candidate.pk)
        self.assertEqual(unchanged_candidate.email, "")
        self.assertEqual(unchanged_task.title, task.title)
        self.assertEqual(unchanged_task.owner_id, account.pk)

    def test_fresh_install_with_no_legacy_tasks_creates_no_marker_account(self):
        old_apps = self.migrate_tasks_to("0001_initial")
        old_apps.get_model("tasks", "Task").objects.all().delete()
        old_apps.get_model("auth", "User").objects.all().delete()

        new_apps = self.migrate_tasks_to("0005_repair_legacy_owner_email")
        user_model = new_apps.get_model("auth", "User")
        task_model = new_apps.get_model("tasks", "Task")

        self.assertEqual(task_model.objects.count(), 0)
        self.assertFalse(
            user_model.objects.filter(email=self.legacy_email).exists()
        )

    def test_eligible_account_without_tasks_is_not_marked(self):
        old_apps = self.migrate_tasks_to("0004_merge_status_and_owner")
        user_model = old_apps.get_model("auth", "User")
        task_model = old_apps.get_model("tasks", "Task")
        self.assertFalse(
            user_model.objects.filter(
                username__regex=r"^legacy-task-owner(-[0-9]+)?$"
            ).exists()
        )

        candidate = user_model.objects.create(
            username="legacy-task-owner",
            email="",
            password="!",
            is_active=False,
        )
        self.assertFalse(task_model.objects.filter(owner_id=candidate.pk).exists())

        new_apps = self.migrate_tasks_to("0005_repair_legacy_owner_email")
        repaired_candidate = new_apps.get_model("auth", "User").objects.get(
            pk=candidate.pk
        )

        self.assertEqual(repaired_candidate.email, "")

    def test_whitespace_email_with_owned_task_is_marked_without_changing_task(self):
        old_apps = self.migrate_tasks_to("0004_merge_status_and_owner")
        user_model = old_apps.get_model("auth", "User")
        task_model = old_apps.get_model("tasks", "Task")
        candidate = user_model.objects.create(
            username="legacy-task-owner",
            email=" ",
            password="!",
            is_active=False,
        )
        task = task_model.objects.create(
            title="Whitespace email legacy task",
            description="Keep task values intact",
            status="in_progress",
            owner_id=candidate.pk,
        )
        original_task = (
            task.title,
            task.description,
            task.status,
            task.owner_id,
        )

        new_apps = self.migrate_tasks_to("0005_repair_legacy_owner_email")
        new_user_model = new_apps.get_model("auth", "User")
        new_task_model = new_apps.get_model("tasks", "Task")
        repaired_candidate = new_user_model.objects.get(pk=candidate.pk)
        unchanged_task = new_task_model.objects.get(pk=task.pk)

        self.assertEqual(repaired_candidate.email, self.legacy_email)
        self.assertEqual(
            (
                unchanged_task.title,
                unchanged_task.description,
                unchanged_task.status,
                unchanged_task.owner_id,
            ),
            original_task,
        )

    def test_whitespace_email_without_owned_tasks_ignores_other_owners_tasks(self):
        old_apps = self.migrate_tasks_to("0004_merge_status_and_owner")
        user_model = old_apps.get_model("auth", "User")
        task_model = old_apps.get_model("tasks", "Task")
        candidate = user_model.objects.create(
            username="legacy-task-owner",
            email=" ",
            password="!",
            is_active=False,
        )
        other_owner = user_model.objects.create(
            username="unrelated-user",
            email="unrelated@example.com",
            password="usable-password-hash",
            is_active=True,
        )
        other_task = task_model.objects.create(
            title="Another user's task",
            description="Not owned by the legacy candidate",
            status="pending",
            owner_id=other_owner.pk,
        )

        new_apps = self.migrate_tasks_to("0005_repair_legacy_owner_email")
        new_user_model = new_apps.get_model("auth", "User")
        new_task_model = new_apps.get_model("tasks", "Task")
        unchanged_candidate = new_user_model.objects.get(pk=candidate.pk)
        unchanged_owner = new_user_model.objects.get(pk=other_owner.pk)
        unchanged_task = new_task_model.objects.get(pk=other_task.pk)

        self.assertEqual(unchanged_candidate.email, " ")
        self.assertEqual(unchanged_owner.email, "unrelated@example.com")
        self.assertEqual(unchanged_task.owner_id, other_owner.pk)
        self.assertEqual(unchanged_task.title, "Another user's task")
        self.assertEqual(
            unchanged_task.description, "Not owned by the legacy candidate"
        )
