import ipaddress
from unittest import mock

import requests
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from huey.contrib.djhuey import HUEY as huey

from bookmarks import queries
from bookmarks.models import Bookmark, BookmarkSearch
from bookmarks.services import bookmarks as bookmark_service
from bookmarks.services import tasks
from bookmarks.services.http_client import BlockedAddressError
from bookmarks.tests.helpers import BookmarkFactoryMixin


class DeadLinkTaskTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        huey.immediate = True
        huey.results = True
        huey.store_none = True

    def tearDown(self):
        huey.storage.flush_results()
        huey.immediate = False

    @staticmethod
    def response(status_code):
        response = mock.Mock(status_code=status_code)
        response.close = mock.Mock()
        return response

    def test_check_link_stores_http_status_and_timestamp(self):
        bookmark = self.setup_bookmark()

        with mock.patch("bookmarks.services.http_client.head") as mock_head:
            mock_head.return_value = self.response(404)

            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status, 404)
        self.assertIsNotNone(bookmark.link_checked_at)
        mock_head.assert_called_once()
        self.assertTrue(mock_head.call_args.kwargs["stream"])
        mock_head.return_value.close.assert_called_once()

    def test_check_link_falls_back_to_streamed_get_for_405(self):
        bookmark = self.setup_bookmark()

        with (
            mock.patch("bookmarks.services.http_client.head") as mock_head,
            mock.patch("bookmarks.services.http_client.get") as mock_get,
        ):
            mock_head.return_value = self.response(405)
            mock_get.return_value = self.response(200)

            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status, 200)
        mock_get.assert_called_once_with(
            bookmark.url,
            headers=mock.ANY,
            timeout=tasks.LINK_CHECK_TIMEOUT,
            allow_redirects=False,
            stream=True,
        )
        mock_get.return_value.close.assert_called_once()

    def test_check_link_stores_unreachable_for_request_errors(self):
        bookmark = self.setup_bookmark()

        with mock.patch(
            "bookmarks.services.http_client.head",
            side_effect=requests.RequestException("DNS failure"),
        ):
            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status, Bookmark.LINK_STATUS_UNREACHABLE)
        self.assertIsNotNone(bookmark.link_checked_at)

    def test_check_link_falls_back_to_streamed_get_for_501(self):
        bookmark = self.setup_bookmark()

        with (
            mock.patch("bookmarks.services.http_client.head") as mock_head,
            mock.patch("bookmarks.services.http_client.get") as mock_get,
        ):
            mock_head.return_value = self.response(501)
            mock_get.return_value = self.response(200)

            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status, 200)
        mock_head.return_value.close.assert_called_once()
        mock_get.return_value.close.assert_called_once()

    def test_check_link_follows_redirects_without_buffering_bodies(self):
        bookmark = self.setup_bookmark(url="https://example.com/start")
        redirect = self.response(302)
        redirect.headers = {"Location": "/final"}
        final = self.response(200)

        with mock.patch("bookmarks.services.http_client.head") as mock_head:
            mock_head.side_effect = [redirect, final]

            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status, 200)
        self.assertEqual(
            [call.args[0] for call in mock_head.call_args_list],
            ["https://example.com/start", "https://example.com/final"],
        )
        self.assertTrue(all(call.kwargs["stream"] for call in mock_head.call_args_list))
        redirect.close.assert_called_once()
        final.close.assert_called_once()

    def test_check_link_stores_blocked_separately_from_unreachable(self):
        bookmark = self.setup_bookmark()
        error = BlockedAddressError("localhost", ipaddress.ip_address("127.0.0.1"))

        with mock.patch(
            "bookmarks.services.http_client.head", side_effect=error
        ) as mock_head:
            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status, Bookmark.LINK_STATUS_BLOCKED)
        mock_head.assert_called_once()

    def test_check_link_clears_previous_broken_status(self):
        bookmark = self.setup_bookmark()
        bookmark.link_status = 404
        bookmark.link_checked_at = timezone.now()
        bookmark.save()

        with mock.patch("bookmarks.services.http_client.head") as mock_head:
            mock_head.return_value = self.response(200)
            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertEqual(bookmark.link_status, 200)

    def test_check_link_does_not_save_result_for_url_changed_during_request(self):
        bookmark = self.setup_bookmark()
        original_url = bookmark.url

        def change_url_during_request(*args, **kwargs):
            bookmark.url = "https://example.org/updated"
            bookmark.save()
            return self.response(404)

        with mock.patch(
            "bookmarks.services.http_client.head",
            side_effect=change_url_during_request,
        ):
            tasks.check_link(bookmark.id)

        bookmark.refresh_from_db()
        self.assertNotEqual(bookmark.url, original_url)
        self.assertIsNone(bookmark.link_status)
        self.assertIsNone(bookmark.link_checked_at)

    def test_editing_url_clears_previous_link_health(self):
        bookmark = self.setup_bookmark()
        bookmark.link_status = 404
        bookmark.link_checked_at = timezone.now()
        bookmark.save()

        bookmark.url = "https://example.org/updated"
        bookmark_service.update_bookmark(bookmark, "", self.user)

        bookmark.refresh_from_db()
        self.assertIsNone(bookmark.link_status)
        self.assertIsNone(bookmark.link_checked_at)

    @override_settings(LD_DISABLE_BACKGROUND_TASKS=True)
    def test_check_link_does_not_queue_when_background_tasks_are_disabled(self):
        bookmark = self.setup_bookmark()

        with mock.patch.object(tasks, "_check_link_task") as mock_task:
            tasks.check_link(bookmark.id)

        mock_task.assert_not_called()


class DeadLinkQueryAndViewTestCase(TestCase, BookmarkFactoryMixin):
    def setUp(self):
        self.user = self.get_or_create_test_user()
        self.client.force_login(self.user)

    def test_broken_search_returns_http_errors_and_unreachable_links(self):
        http_broken = self.setup_bookmark()
        http_broken.link_status = 404
        http_broken.save()
        unreachable = self.setup_bookmark()
        unreachable.link_status = Bookmark.LINK_STATUS_UNREACHABLE
        unreachable.save()
        self.setup_bookmark()
        blocked = self.setup_bookmark()
        blocked.link_status = Bookmark.LINK_STATUS_BLOCKED
        blocked.save()

        result = queries.query_bookmarks(
            self.user, self.user.profile, BookmarkSearch(q="!broken")
        )

        self.assertCountEqual(list(result), [http_broken, unreachable])

    def test_broken_search_combines_with_tags_and_legacy_search(self):
        tag = self.setup_tag(name="python")
        broken_tagged = self.setup_bookmark(tags=[tag])
        broken_tagged.link_status = 404
        broken_tagged.save()
        broken_untagged = self.setup_bookmark()
        broken_untagged.link_status = 500
        broken_untagged.save()
        healthy_tagged = self.setup_bookmark(tags=[tag])
        healthy_tagged.link_status = 200
        healthy_tagged.save()

        result = queries.query_bookmarks(
            self.user,
            self.user.profile,
            BookmarkSearch(q="!broken #python"),
        )
        self.assertCountEqual(list(result), [broken_tagged])

        self.user.profile.legacy_search = True
        self.user.profile.save()
        legacy_result = queries.query_bookmarks(
            self.user,
            self.user.profile,
            BookmarkSearch(q="!broken"),
        )
        self.assertCountEqual(list(legacy_result), [broken_tagged, broken_untagged])

    def test_bulk_check_queues_only_selected_owned_bookmarks(self):
        bookmark = self.setup_bookmark()
        other_bookmark = self.setup_bookmark()
        other_user = self.setup_user()
        other_bookmark.owner = other_user
        other_bookmark.save()

        with mock.patch("bookmarks.services.tasks.check_link") as mock_check:
            response = self.client.post(
                reverse("linkding:bookmarks.index.action"),
                {
                    "bulk_action": "bulk_check",
                    "bulk_execute": "",
                    "bookmark_id": [str(bookmark.id), str(other_bookmark.id)],
                },
            )

        self.assertEqual(response.status_code, 302)
        mock_check.assert_called_once_with(bookmark.id)

    def test_broken_bookmark_renders_badge_with_status_tooltip(self):
        bookmark = self.setup_bookmark()
        bookmark.link_status = 404
        bookmark.link_checked_at = timezone.now()
        bookmark.save()

        response = self.client.get(reverse("linkding:bookmarks.index"))
        html = response.content.decode()

        self.assertIn('data-badge="broken"', html)
        self.assertIn("HTTP 404; checked Today", html)
