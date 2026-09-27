from datetime import datetime, timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from huey.contrib.djhuey import HUEY as huey

from bookmarks import queries
from bookmarks.api.serializers import BookmarkSerializer
from bookmarks.models import Bookmark, BookmarkSearch
from bookmarks.services import bookmarks as bookmark_service
from bookmarks.services import tasks
from bookmarks.tests.helpers import BookmarkFactoryMixin, LinkdingApiTestCase


class ReminderTaskTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        huey.immediate = True
        huey.results = True
        huey.store_none = True

    def tearDown(self):
        huey.storage.flush_results()
        huey.immediate = False

    def test_due_reminders_unarchive_mark_unread_and_clear(self):
        now = timezone.now()
        due = self.setup_bookmark(
            is_archived=True,
            unread=False,
            remind_at=now - timedelta(minutes=1),
        )
        future = self.setup_bookmark(
            is_archived=True,
            remind_at=now + timedelta(days=1),
        )

        with mock.patch("bookmarks.services.tasks.timezone.now", return_value=now):
            tasks.process_due_bookmark_reminders()

        due.refresh_from_db()
        future.refresh_from_db()
        self.assertFalse(due.is_archived)
        self.assertTrue(due.unread)
        self.assertIsNone(due.remind_at)
        self.assertTrue(future.is_archived)
        self.assertEqual(future.remind_at, now + timedelta(days=1))

    def test_due_reminder_task_is_idempotent(self):
        initial_now = timezone.now()
        later_now = initial_now + timedelta(minutes=5)
        bookmark = self.setup_bookmark(
            is_archived=True,
            remind_at=initial_now - timedelta(minutes=1),
        )

        with mock.patch(
            "bookmarks.services.tasks.timezone.now", return_value=initial_now
        ):
            tasks.process_due_bookmark_reminders()
        bookmark.refresh_from_db()
        first_modified = bookmark.date_modified

        with mock.patch(
            "bookmarks.services.tasks.timezone.now", return_value=later_now
        ):
            tasks.process_due_bookmark_reminders()
        bookmark.refresh_from_db()

        self.assertEqual(bookmark.date_modified, first_modified)
        self.assertIsNone(bookmark.remind_at)

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_due_reminder_task_is_disabled_with_background_tasks(self):
        bookmark = self.setup_bookmark(
            is_archived=True,
            remind_at=timezone.now() - timedelta(minutes=1),
        )

        tasks.process_due_bookmark_reminders()

        bookmark.refresh_from_db()
        self.assertTrue(bookmark.is_archived)
        self.assertIsNotNone(bookmark.remind_at)


class SnoozeServiceAndQueryTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.user = self.get_or_create_test_user()

    def test_snooze_bookmarks_archives_and_sets_one_week_reminder(self):
        now = timezone.now()
        bookmark = self.setup_bookmark()
        other_user = self.setup_user()
        other_bookmark = self.setup_bookmark(user=other_user)

        with mock.patch("bookmarks.services.bookmarks.timezone.now", return_value=now):
            bookmark_service.snooze_bookmarks(
                [bookmark.id, other_bookmark.id], self.user
            )

        bookmark.refresh_from_db()
        other_bookmark.refresh_from_db()
        self.assertTrue(bookmark.is_archived)
        self.assertEqual(bookmark.remind_at, now + timedelta(days=7))
        self.assertFalse(other_bookmark.is_archived)
        self.assertIsNone(other_bookmark.remind_at)

    def test_unarchive_cancels_existing_reminder(self):
        bookmark = self.setup_bookmark(
            is_archived=True,
            remind_at=timezone.now() + timedelta(days=1),
        )

        bookmark_service.unarchive_bookmark(bookmark)

        bookmark.refresh_from_db()
        self.assertFalse(bookmark.is_archived)
        self.assertIsNone(bookmark.remind_at)

    def test_modern_search_combines_reminder_keyword_with_term(self):
        now = timezone.now()
        matching = self.setup_bookmark(
            title="python reminder", remind_at=now + timedelta(days=1)
        )
        self.setup_bookmark(title="python now")
        self.setup_bookmark(
            title="javascript reminder", remind_at=now + timedelta(days=1)
        )

        with mock.patch("bookmarks.queries.timezone.now", return_value=now):
            results = queries.query_bookmarks(
                self.user,
                self.user.profile,
                BookmarkSearch(q="!snoozed python"),
            )

        self.assertEqual(list(results), [matching])

    def test_search_due_and_snoozed_bookmarks(self):
        now = timezone.make_aware(datetime(2026, 1, 15, 12, 0))
        due = self.setup_bookmark(
            is_archived=False, remind_at=now - timedelta(seconds=1)
        )
        due_today = self.setup_bookmark(
            is_archived=False, remind_at=now + timedelta(hours=1)
        )
        snoozed = self.setup_bookmark(
            is_archived=True, remind_at=now + timedelta(days=1)
        )
        self.setup_bookmark()

        with mock.patch("bookmarks.queries.timezone.now", return_value=now):
            due_results = queries.query_bookmarks(
                self.user, self.user.profile, BookmarkSearch(q="!due")
            )
            snoozed_results = queries.query_archived_bookmarks(
                self.user, self.user.profile, BookmarkSearch(q="!snoozed")
            )

        self.assertCountEqual(list(due_results), [due, due_today])
        self.assertEqual(list(snoozed_results), [snoozed])

        self.user.profile.legacy_search = True
        self.user.profile.save()
        with mock.patch("bookmarks.queries.timezone.now", return_value=now):
            legacy_due = queries.query_bookmarks(
                self.user, self.user.profile, BookmarkSearch(q="!due")
            )
        self.assertCountEqual(list(legacy_due), [due, due_today])


class SnoozeViewTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.user = self.get_or_create_test_user()
        self.client.force_login(self.user)

    def test_bulk_snooze_archives_selected_bookmark(self):
        bookmark = self.setup_bookmark()

        response = self.client.post(
            reverse("linkding:bookmarks.index.action"),
            {
                "bulk_action": "bulk_snooze",
                "bulk_execute": "",
                "bookmark_id": [str(bookmark.id)],
            },
        )

        self.assertEqual(response.status_code, 302)
        bookmark.refresh_from_db()
        self.assertTrue(bookmark.is_archived)
        self.assertIsNotNone(bookmark.remind_at)

    def test_archived_list_displays_reminder_date(self):
        bookmark = self.setup_bookmark(
            is_archived=True,
            remind_at=timezone.now() + timedelta(days=1),
        )

        response = self.client.get(reverse("linkding:bookmarks.archived"))

        self.assertContains(response, "Remind")
        self.assertContains(response, 'title="Reminder scheduled"')
        self.assertContains(response, bookmark.title)

    def test_shared_bookmark_does_not_expose_owner_reminder(self):
        owner = self.setup_user(enable_sharing=True, enable_public_sharing=True)
        self.setup_bookmark(
            user=owner,
            shared=True,
            remind_at=timezone.now() + timedelta(days=1),
        )

        response = self.client.get(reverse("linkding:bookmarks.shared"))

        self.assertNotContains(response, "Reminder scheduled")

    def test_reminder_form_can_set_and_clear_reminder(self):
        bookmark = self.setup_bookmark()
        reminder = (timezone.localtime(timezone.now()) + timedelta(days=1)).replace(
            second=0, microsecond=0
        )
        reminder_value = reminder.strftime("%Y-%m-%dT%H:%M")
        edit_url = reverse("linkding:bookmarks.edit", args=[bookmark.id])

        response = self.client.post(
            edit_url,
            {
                "url": bookmark.url,
                "tag_string": "",
                "title": bookmark.title,
                "description": bookmark.description,
                "notes": bookmark.notes,
                "remind_at": reminder_value,
                "unread": "",
                "shared": "",
                "auto_close": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        bookmark.refresh_from_db()
        self.assertIsNotNone(bookmark.remind_at)
        self.assertTrue(bookmark.is_archived)

        response = self.client.post(
            edit_url,
            {
                "url": bookmark.url,
                "tag_string": "",
                "title": bookmark.title,
                "description": bookmark.description,
                "notes": bookmark.notes,
                "remind_at": "",
                "unread": "",
                "shared": "",
                "auto_close": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        bookmark.refresh_from_db()
        self.assertIsNone(bookmark.remind_at)

    def test_api_hides_reminder_for_shared_bookmark(self):
        bookmark = self.setup_bookmark(
            shared=True,
            remind_at=timezone.now() + timedelta(days=1),
        )
        request = mock.Mock(user=self.setup_user())

        data = BookmarkSerializer(bookmark, context={"request": request}).data

        self.assertNotIn("remind_at", data)


class SnoozeApiTestCase(LinkdingApiTestCase, BookmarkFactoryMixin):
    def test_api_can_create_update_and_clear_reminder(self):
        self.authenticate()
        remind_at = timezone.now() + timedelta(days=2)
        data = {
            "url": "https://example.com/reminder-api",
            "remind_at": remind_at.isoformat(),
        }

        response = self.post(
            reverse("linkding:bookmark-list") + "?disable_scraping",
            data,
            expected_status_code=201,
        )
        bookmark = Bookmark.objects.get(url=data["url"])
        self.assertEqual(
            response.data["remind_at"],
            bookmark.remind_at.isoformat().replace("+00:00", "Z"),
        )
        self.assertTrue(bookmark.is_archived)

        detail_url = reverse("linkding:bookmark-detail", args=[bookmark.id])
        response = self.patch(detail_url, {"remind_at": None})
        bookmark.refresh_from_db()
        self.assertIsNone(response.data["remind_at"])
        self.assertIsNone(bookmark.remind_at)
