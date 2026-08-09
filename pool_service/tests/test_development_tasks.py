from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
    Organization,
    OrganizationAccess,
)
from pool_service import development_views


class DevelopmentTaskTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Организация разработки",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = self.user_with_role("dev-owner", "owner")
        self.admin = self.user_with_role("dev-admin", "admin")
        self.manager = self.user_with_role("dev-manager", "manager")
        self.accountant = self.user_with_role("dev-accountant", "accountant")
        self.service = self.user_with_role("dev-service", "service")
        self.installer = self.user_with_role("dev-installer", "installer")

    def user_with_role(self, username, role, *, organization=None, is_superuser=False):
        user = User.objects.create_user(
            username,
            password="test-password",
            is_superuser=is_superuser,
            is_staff=is_superuser,
        )
        OrganizationAccess.objects.create(
            user=user,
            organization=organization or self.organization,
            role=role,
        )
        return user

    def create_task(self, *, organization=None, initiator=None, title="Импорт из 1С"):
        return DevelopmentTask.objects.create(
            organization=organization or self.organization,
            initiator=initiator or self.owner,
            title=title,
            description="Исходное техническое задание",
            business_goal="Снизить ручной труд",
            definition_of_done="Тесты проходят",
            priority=DevelopmentTask.PRIORITY_HIGH,
        )

    def task_create_payload(self):
        return {
            "title": "Новая задача",
            "description": "Подробное описание",
            "business_goal": "Проверяемая бизнес-цель",
            "priority": DevelopmentTask.PRIORITY_HIGH,
            "definition_of_done": "Определённый результат",
        }

    def task_update_payload(self, **overrides):
        payload = {
            "priority": DevelopmentTask.PRIORITY_HIGH,
            "status": DevelopmentTask.STATUS_TESTING,
            "current_stage": DevelopmentTask.STAGE_TESTING,
            "completed_work": "Код написан",
            "current_activity": "Выполняются тесты",
            "blockers": "",
            "final_summary": "",
            "execution_result": "",
        }
        payload.update(overrides)
        return payload

    def iteration_payload(self, **overrides):
        payload = {
            "executor_type": DevelopmentIteration.EXECUTOR_CODEX,
            "status": DevelopmentIteration.STATUS_REVIEW,
            "prompt": "Исправь классификацию показателей.",
            "response": "Исправление реализовано.",
            "result_summary": "Парсер различает показатели.",
            "started_at": "",
            "completed_at": "",
            "changed_files": "pool_service/parser.py",
            "test_result": "3 passed / 1 failed",
            "tests_passed": 3,
            "tests_failed": 1,
            "technical_errors": "Один регрессионный тест",
            "reviewer_notes": "Нужна доработка",
            "next_prompt": "Исправь регрессию.",
        }
        payload.update(overrides)
        return payload

    def test_owner_admin_and_superuser_with_organization_can_access(self):
        superuser = self.user_with_role("dev-root", "admin", is_superuser=True)
        for user in (self.owner, self.admin, superuser):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                response = self.client.get(reverse("development_task_list"))
                self.assertEqual(response.status_code, 200)

    def test_user_without_administrative_role_is_denied(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("development_task_list"))
        self.assertEqual(response.status_code, 403)

    def test_anonymous_user_is_redirected_to_login(self):
        response = self.client.get(reverse("development_task_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_anonymous_user_cannot_post_changes(self):
        task = self.create_task()
        responses = [
            self.client.post(reverse("development_task_create"), self.task_create_payload()),
            self.client.post(
                reverse("development_task_update", args=[task.pk]),
                self.task_update_payload(),
            ),
            self.client.post(
                reverse("development_iteration_create", args=[task.pk]),
                self.iteration_payload(),
            ),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 302)
            self.assertIn("/accounts/login/", response.url)
        self.assertFalse(task.iterations.exists())

    def test_superuser_without_organization_context_is_denied(self):
        superuser = User.objects.create_superuser(
            "root-without-organization",
            password="test-password",
            email="root@example.test",
        )
        self.client.force_login(superuser)
        self.assertEqual(
            self.client.get(reverse("development_task_list")).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("development_task_create"), self.task_create_payload()
            ).status_code,
            403,
        )

    def test_task_creation_scopes_organization_and_creates_event(self):
        self.client.force_login(self.owner)
        response = self.client.post(reverse("development_task_create"), self.task_create_payload())
        task = DevelopmentTask.objects.get(title="Новая задача")

        self.assertRedirects(response, reverse("development_task_detail", args=[task.pk]))
        self.assertEqual(task.organization, self.organization)
        self.assertEqual(task.initiator, self.owner)
        self.assertEqual(task.reference, f"DEV-{task.pk:04d}")
        self.assertTrue(
            task.events.filter(event_type=DevelopmentTaskEvent.TYPE_CREATED).exists()
        )

    def test_iteration_creation_and_task_relation(self):
        task = self.create_task()
        self.client.force_login(self.admin)
        response = self.client.post(
            reverse("development_iteration_create", args=[task.pk]),
            self.iteration_payload(),
        )
        iteration = task.iterations.get()

        self.assertRedirects(response, reverse("development_task_detail", args=[task.pk]))
        self.assertEqual(iteration.iteration_number, 1)
        self.assertEqual(iteration.tests_passed, 3)
        self.assertEqual(iteration.tests_failed, 1)
        self.assertEqual(iteration.task, task)
        self.assertTrue(
            task.events.filter(event_type=DevelopmentTaskEvent.TYPE_ITERATION_ADDED).exists()
        )
        self.assertTrue(
            task.events.filter(event_type=DevelopmentTaskEvent.TYPE_TEST_RESULT).exists()
        )

    def test_iteration_numbers_increment_per_task(self):
        task = self.create_task()
        DevelopmentIteration.objects.create(task=task, iteration_number=1)
        self.client.force_login(self.owner)
        self.client.post(
            reverse("development_iteration_create", args=[task.pk]),
            self.iteration_payload(tests_passed=0, tests_failed=0, test_result=""),
        )
        self.assertEqual(list(task.iterations.values_list("iteration_number", flat=True)), [1, 2])

    def test_first_iteration_locks_parent_task_inside_atomic_block(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        lock_calls = []
        real_getter = development_views._task_for_organization

        def recording_getter(organization, task_id, *, lock=False):
            lock_calls.append((lock, connection.in_atomic_block))
            return real_getter(organization, task_id, lock=lock)

        with patch(
            "pool_service.development_views._task_for_organization",
            side_effect=recording_getter,
        ):
            response = self.client.post(
                reverse("development_iteration_create", args=[task.pk]),
                self.iteration_payload(),
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn((True, True), lock_calls)
        self.assertEqual(task.iterations.get().iteration_number, 1)

    def test_owner_starts_new_task_with_system_iteration_and_audit_event(self):
        task = self.create_task(title="Запуск внутреннего анализа")
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("development_task_start", args=[task.pk]),
            {
                "status": DevelopmentTask.STATUS_DONE,
                "current_stage": DevelopmentTask.STAGE_COMPLETION,
                "organization": 999999,
                "iteration_number": 777,
                "executor": self.admin.pk,
                "automation_metadata": '{"spoofed": true}',
            },
        )
        task.refresh_from_db()
        iteration = task.iterations.get()
        event = task.events.get(
            event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED
        )

        self.assertRedirects(
            response, reverse("development_task_detail", args=[task.pk])
        )
        self.assertEqual(task.status, DevelopmentTask.STATUS_ANALYSIS)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_ANALYSIS)
        self.assertIsNotNone(task.started_at)
        self.assertEqual(
            task.current_activity, "Выполняется первичный анализ задачи"
        )
        self.assertEqual(iteration.iteration_number, 1)
        self.assertEqual(
            iteration.executor_type, DevelopmentIteration.EXECUTOR_SYSTEM
        )
        self.assertIsNone(iteration.executor)
        self.assertEqual(iteration.status, DevelopmentIteration.STATUS_WORKING)
        self.assertIsNotNone(iteration.started_at)
        self.assertEqual(iteration.automation_metadata, {})
        for expected in (
            task.reference,
            task.title,
            task.description,
            task.business_goal,
            task.definition_of_done,
            task.get_priority_display(),
            "первичный технический анализ",
            "Не выполняй deploy",
        ):
            self.assertIn(expected, iteration.prompt)
        self.assertEqual(event.actor, self.owner)
        self.assertEqual(event.message, "Задача запущена")
        self.assertEqual(event.metadata["old_status"], DevelopmentTask.STATUS_NEW)
        self.assertEqual(
            event.metadata["new_status"], DevelopmentTask.STATUS_ANALYSIS
        )
        self.assertEqual(event.metadata["iteration_id"], iteration.pk)
        self.assertEqual(event.metadata["iteration_number"], 1)
        self.assertEqual(event.metadata["action"], "start")

    def test_admin_can_start_new_task(self):
        task = self.create_task()
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("development_task_start", args=[task.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(task.iterations.count(), 1)
        self.assertEqual(
            task.events.get(
                event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED
            ).actor,
            self.admin,
        )

    def test_superuser_with_organization_can_start_new_task(self):
        superuser = self.user_with_role("dev-start-root", "admin", is_superuser=True)
        task = self.create_task()
        self.client.force_login(superuser)

        response = self.client.post(
            reverse("development_task_start", args=[task.pk])
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(task.iterations.count(), 1)

    def test_repeated_start_is_idempotent(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        url = reverse("development_task_start", args=[task.pk])

        first_response = self.client.post(url)
        task.refresh_from_db()
        first_started_at = task.started_at
        first_activity = task.current_activity
        second_response = self.client.post(url, follow=True)
        task.refresh_from_db()

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 200)
        self.assertContains(
            second_response, "Задача уже запущена или недоступна для запуска."
        )
        self.assertEqual(task.iterations.count(), 1)
        self.assertEqual(
            task.events.filter(
                event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
                metadata__action="start",
            ).count(),
            1,
        )
        self.assertEqual(task.started_at, first_started_at)
        self.assertEqual(task.current_activity, first_activity)

    def test_start_locks_parent_task_inside_atomic_block(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        lock_calls = []
        real_getter = development_views._task_for_organization

        def recording_getter(organization, task_id, *, lock=False):
            lock_calls.append((lock, connection.in_atomic_block))
            return real_getter(organization, task_id, lock=lock)

        with patch(
            "pool_service.development_views._task_for_organization",
            side_effect=recording_getter,
        ):
            response = self.client.post(
                reverse("development_task_start", args=[task.pk])
            )

        self.assertEqual(response.status_code, 302)
        self.assertIn((True, True), lock_calls)
        self.assertEqual(task.iterations.count(), 1)

    def test_start_uses_next_iteration_number_after_manual_iteration(self):
        task = self.create_task()
        DevelopmentIteration.objects.create(
            task=task,
            iteration_number=1,
            executor_type=DevelopmentIteration.EXECUTOR_HUMAN,
        )
        self.client.force_login(self.owner)

        self.client.post(reverse("development_task_start", args=[task.pk]))

        self.assertEqual(
            list(task.iterations.values_list("iteration_number", flat=True)),
            [1, 2],
        )
        self.assertEqual(
            task.iterations.get(iteration_number=2).executor_type,
            DevelopmentIteration.EXECUTOR_SYSTEM,
        )

    def test_cross_organization_cannot_start_task_by_post(self):
        other_org = Organization.objects.create(
            name="Другая организация запуска",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_admin = self.user_with_role(
            "other-start-admin", "admin", organization=other_org
        )
        task = self.create_task()
        self.client.force_login(other_admin)

        response = self.client.post(
            reverse("development_task_start", args=[task.pk])
        )
        task.refresh_from_db()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(task.status, DevelopmentTask.STATUS_NEW)
        self.assertFalse(task.iterations.exists())
        self.assertFalse(task.events.exists())

    def test_non_administrative_roles_cannot_start_task(self):
        task = self.create_task()
        url = reverse("development_task_start", args=[task.pk])

        for user in (self.manager, self.accountant, self.service, self.installer):
            with self.subTest(role=user.username):
                self.client.force_login(user)
                response = self.client.post(url)
                self.assertIn(response.status_code, {302, 403})

        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_NEW)
        self.assertFalse(task.iterations.exists())
        self.assertFalse(task.events.exists())

    def test_anonymous_cannot_start_task_and_get_is_not_allowed(self):
        task = self.create_task()
        url = reverse("development_task_start", args=[task.pk])

        anonymous_response = self.client.post(url)
        self.client.force_login(self.owner)
        get_response = self.client.get(url)
        task.refresh_from_db()

        self.assertEqual(anonymous_response.status_code, 302)
        self.assertIn("/accounts/login/", anonymous_response.url)
        self.assertEqual(get_response.status_code, 405)
        self.assertEqual(task.status, DevelopmentTask.STATUS_NEW)
        self.assertFalse(task.iterations.exists())

    def test_start_rolls_back_task_and_iteration_when_event_fails(self):
        task = self.create_task()
        self.client.force_login(self.owner)

        with patch(
            "pool_service.development_views.DevelopmentTaskEvent.objects.create",
            side_effect=RuntimeError("event unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("development_task_start", args=[task.pk])
                )

        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_NEW)
        self.assertEqual(task.current_stage, DevelopmentTask.STAGE_ANALYSIS)
        self.assertIsNone(task.started_at)
        self.assertEqual(task.current_activity, "")
        self.assertFalse(task.iterations.exists())
        self.assertFalse(task.events.exists())

    def test_start_button_is_visible_only_for_new_task(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        detail_url = reverse("development_task_detail", args=[task.pk])
        start_url = reverse("development_task_start", args=[task.pk])

        before = self.client.get(detail_url)
        self.assertContains(before, "▶ Запустить")
        self.assertContains(before, 'method="post"', html=False)
        self.assertContains(before, f'action="{start_url}"', html=False)
        self.assertContains(before, "csrfmiddlewaretoken")

        self.client.post(start_url)
        after = self.client.get(detail_url)

        self.assertNotContains(after, "▶ Запустить")
        self.assertContains(after, "Анализ")
        self.assertContains(after, "Выполняется первичный анализ задачи")
        self.assertContains(after, "Система")
        self.assertContains(after, "Задача запущена")

    def test_iteration_number_has_database_unique_constraint(self):
        task = self.create_task()
        DevelopmentIteration.objects.create(task=task, iteration_number=1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            DevelopmentIteration.objects.create(task=task, iteration_number=1)

    def test_status_update_sets_dates_and_event(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("development_task_update", args=[task.pk]),
            self.task_update_payload(
                status=DevelopmentTask.STATUS_DONE,
                current_stage=DevelopmentTask.STAGE_COMPLETION,
                final_summary="Работа завершена",
                execution_result="Функция доступна",
            ),
        )
        task.refresh_from_db()

        self.assertRedirects(response, reverse("development_task_detail", args=[task.pk]))
        self.assertEqual(task.status, DevelopmentTask.STATUS_DONE)
        self.assertIsNotNone(task.started_at)
        self.assertIsNotNone(task.completed_at)
        event = task.events.get(event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED)
        self.assertEqual(event.metadata["new_status"], DevelopmentTask.STATUS_DONE)

    def test_return_from_terminal_status_clears_completed_date(self):
        task = self.create_task()
        task.status = DevelopmentTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.save()
        self.client.force_login(self.owner)
        self.client.post(
            reverse("development_task_update", args=[task.pk]),
            self.task_update_payload(status=DevelopmentTask.STATUS_REVISION),
        )
        task.refresh_from_db()
        self.assertIsNone(task.completed_at)

    def test_list_and_detail_pages_show_task_and_real_stages(self):
        task = self.create_task(title="Контроль себестоимости")
        task.status = DevelopmentTask.STATUS_TESTING
        task.current_stage = DevelopmentTask.STAGE_TESTING
        task.save()
        DevelopmentIteration.objects.create(task=task, iteration_number=1, tests_passed=10)
        self.client.force_login(self.owner)

        list_response = self.client.get(reverse("development_task_list"))
        detail_response = self.client.get(reverse("development_task_detail", args=[task.pk]))

        self.assertContains(list_response, "Контроль себестоимости")
        self.assertContains(list_response, task.reference)
        self.assertContains(detail_response, "Анализ")
        self.assertContains(detail_response, "Тестирование")
        self.assertContains(detail_response, "Итерация #1")
        self.assertNotContains(detail_response, "% готов")

    def test_status_filter(self):
        self.create_task(title="Новая")
        done = self.create_task(title="Завершённая")
        done.status = DevelopmentTask.STATUS_DONE
        done.save()
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("development_task_list"), {"status": DevelopmentTask.STATUS_DONE}
        )
        self.assertContains(response, "Завершённая")
        self.assertNotContains(response, ">Новая</a>", html=False)

    def test_cross_organization_task_is_hidden(self):
        other_org = Organization.objects.create(
            name="Другая организация",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_admin = self.user_with_role("other-admin", "admin", organization=other_org)
        foreign_task = self.create_task(title="Секретная задача")
        self.client.force_login(other_admin)

        task_list = self.client.get(reverse("development_task_list"))
        detail = self.client.get(reverse("development_task_detail", args=[foreign_task.pk]))
        update = self.client.post(
            reverse("development_task_update", args=[foreign_task.pk]),
            self.task_update_payload(),
        )
        iteration = self.client.get(
            reverse("development_iteration_create", args=[foreign_task.pk])
        )

        self.assertNotContains(task_list, "Секретная задача")
        self.assertEqual(detail.status_code, 404)
        self.assertEqual(update.status_code, 404)
        self.assertEqual(iteration.status_code, 404)

    def test_cross_organization_cannot_create_iteration_by_post(self):
        other_org = Organization.objects.create(
            name="Другая организация для POST",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_admin = self.user_with_role(
            "other-post-admin", "admin", organization=other_org
        )
        foreign_task = self.create_task(title="Чужая задача для POST")
        self.client.force_login(other_admin)

        response = self.client.post(
            reverse("development_iteration_create", args=[foreign_task.pk]),
            self.iteration_payload(),
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(
            DevelopmentIteration.objects.filter(task=foreign_task).exists()
        )
        self.assertFalse(foreign_task.events.exists())

    def test_manager_cannot_create_or_update(self):
        task = self.create_task()
        self.client.force_login(self.manager)
        create = self.client.post(reverse("development_task_create"), self.task_create_payload())
        update = self.client.post(
            reverse("development_task_update", args=[task.pk]), self.task_update_payload()
        )
        add_iteration = self.client.post(
            reverse("development_iteration_create", args=[task.pk]), self.iteration_payload()
        )
        self.assertEqual(create.status_code, 403)
        self.assertEqual(update.status_code, 403)
        self.assertEqual(add_iteration.status_code, 403)
        self.assertEqual(DevelopmentTask.objects.count(), 1)
        self.assertFalse(DevelopmentIteration.objects.exists())

    def test_all_non_administrative_roles_are_denied_for_get_and_post(self):
        task = self.create_task()
        for user in (self.manager, self.accountant, self.service, self.installer):
            with self.subTest(role=user.username):
                self.client.force_login(user)
                responses = [
                    self.client.get(reverse("development_task_list")),
                    self.client.post(
                        reverse("development_task_create"), self.task_create_payload()
                    ),
                    self.client.post(
                        reverse("development_task_update", args=[task.pk]),
                        self.task_update_payload(),
                    ),
                    self.client.post(
                        reverse("development_iteration_create", args=[task.pk]),
                        self.iteration_payload(),
                    ),
                ]
                for response in responses:
                    self.assertIn(response.status_code, {302, 403})
                    if response.status_code == 302:
                        self.assertEqual(response.url, reverse("finance_dashboard"))
                self.assertFalse(task.iterations.exists())

    def test_server_owned_fields_cannot_be_mass_assigned(self):
        other_org = Organization.objects.create(
            name="Организация для подмены",
            paid_until=timezone.now() + timedelta(days=30),
        )
        other_user = self.user_with_role("spoofed-user", "admin", organization=other_org)
        self.client.force_login(self.owner)
        create_payload = self.task_create_payload()
        create_payload.update(
            {
                "organization": other_org.pk,
                "initiator": other_user.pk,
                "status": DevelopmentTask.STATUS_DONE,
                "automation_metadata": '{"run_id": "spoofed"}',
                "created_at": "2000-01-01T00:00",
            }
        )
        self.client.post(reverse("development_task_create"), create_payload)
        task = DevelopmentTask.objects.get(title="Новая задача")

        self.assertEqual(task.organization, self.organization)
        self.assertEqual(task.initiator, self.owner)
        self.assertEqual(task.status, DevelopmentTask.STATUS_NEW)
        self.assertEqual(task.automation_metadata, {})

        iteration_payload = self.iteration_payload()
        iteration_payload.update(
            {
                "task": 999999,
                "iteration_number": 777,
                "executor": other_user.pk,
                "automation_metadata": '{"thread_id": "spoofed"}',
            }
        )
        self.client.post(
            reverse("development_iteration_create", args=[task.pk]),
            iteration_payload,
        )
        iteration = task.iterations.get()
        self.assertEqual(iteration.task, task)
        self.assertEqual(iteration.iteration_number, 1)
        self.assertIsNone(iteration.executor)
        self.assertEqual(iteration.automation_metadata, {})

        update_payload = self.task_update_payload()
        update_payload.update(
            {
                "organization": other_org.pk,
                "initiator": other_user.pk,
                "automation_metadata": '{"run_id": "changed"}',
            }
        )
        self.client.post(
            reverse("development_task_update", args=[task.pk]), update_payload
        )
        task.refresh_from_db()
        self.assertEqual(task.organization, self.organization)
        self.assertEqual(task.initiator, self.owner)
        self.assertEqual(task.automation_metadata, {})

    def test_task_and_initial_event_are_atomic(self):
        self.client.force_login(self.owner)
        with patch(
            "pool_service.development_views.DevelopmentTaskEvent.objects.create",
            side_effect=RuntimeError("event unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("development_task_create"), self.task_create_payload()
                )
        self.assertFalse(DevelopmentTask.objects.filter(title="Новая задача").exists())

    def test_iteration_and_event_are_atomic(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        with patch(
            "pool_service.development_views.DevelopmentTaskEvent.objects.create",
            side_effect=RuntimeError("event unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("development_iteration_create", args=[task.pk]),
                    self.iteration_payload(),
                )
        self.assertFalse(task.iterations.exists())
        self.assertFalse(task.events.exists())

    def test_status_change_and_event_are_atomic(self):
        task = self.create_task()
        self.client.force_login(self.owner)
        with patch(
            "pool_service.development_views.DevelopmentTaskEvent.objects.create",
            side_effect=RuntimeError("event unavailable"),
        ):
            with self.assertRaises(RuntimeError):
                self.client.post(
                    reverse("development_task_update", args=[task.pk]),
                    self.task_update_payload(status=DevelopmentTask.STATUS_DONE),
                )
        task.refresh_from_db()
        self.assertEqual(task.status, DevelopmentTask.STATUS_NEW)
        self.assertIsNone(task.started_at)
        self.assertIsNone(task.completed_at)
        self.assertFalse(task.events.exists())

    def test_reference_is_stable_derived_from_primary_key_and_query_free(self):
        task = self.create_task()
        reference = task.reference
        task.title = "Изменённое название"
        task.save()
        task.refresh_from_db()

        self.assertEqual(task.reference, reference)
        self.assertEqual(reference, f"DEV-{task.pk:04d}")
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(task.reference, reference)
        self.assertEqual(len(queries), 0)

        large_task = DevelopmentTask.objects.create(
            id=10000,
            organization=self.organization,
            initiator=self.owner,
            title="Задача после 9999",
            description="Проверка расширения номера",
        )
        self.assertEqual(large_task.reference, "DEV-10000")
