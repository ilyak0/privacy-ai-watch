import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notify

DAY = dt.date(2026, 10, 17)


def make_item(n=1, title=None, published="2026-10-16T08:00:00Z"):
    url = f"https://example.test/story/{n}"
    return {"canonical_url": url, "url": url, "title": title or f"Story {n}",
            "published_utc": published, "first_seen_utc": "2026-10-17T00:00:00Z",
            "topics": ["privacy"], "regions": ["world"], "source_ids": ["s"],
            "provenance": {}, "source_kind": "news"}


def report(items=(), health=()):
    buckets = {x: [] for x in notify.STREAMS}
    buckets["privacy_world"] = list(items)
    return {"generated_at": "2026-10-17T08:00:00Z", "publication_window_days": 30,
            "recent_unique_items": len(items), "buckets": buckets, "undated": [],
            "source_health": list(health)}


class FakeGitHub:
    def __init__(self, repo, token):
        self.repo, self.token = repo, token
        self.issues = {}
        self.posts = 0
    def daily_issue(self, day):
        return self.issues.get(day.isoformat())
    def create_issue(self, day, body):
        self.posts += 1
        issue = {"html_url": f"https://github.com/o/r/issues/{self.posts}", "body": body}
        self.issues[day.isoformat()] = issue
        return issue


class NotifyTests(unittest.TestCase):
    def test_bulk_email_links_reject_userinfo_and_malformed_hosts(self):
        for url in ("https://user:pass@example.test/story", "https://[broken", "javascript:alert(1)"):
            self.assertEqual(notify._safe_url(url), "")

    def test_short_briefing_precedes_posts_and_full_archive(self):
        row = {**make_item(title="CNIL fines Acme over cookie consent"), "source_kind": "primary"}
        body = notify.build_body(report([row]), [row], DAY)
        self.assertLess(body.index("### Сначала открыть"), body.index("### Вопросы для постов"))
        self.assertLess(body.index("### Вопросы для постов"), body.index("<summary>Все новые публикации"))
        self.assertEqual(body.count("<!-- item:"), 1)
        self.assertIn("Кейс для проверки", body)

    def test_issue_creation_has_no_assignment_notification(self):
        client = notify.GitHub('example/repo', 'fake')
        with patch.object(client, 'request', return_value={'html_url': 'https://example.test/issue'}) as request:
            client.create_issue(DAY, 'body with @ilyak0')
        payload = request.call_args.args[2]
        self.assertNotIn('assignees', payload)
        self.assertIn('@ilyak0', payload['body'])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rp, self.sp = self.root / "latest.json", self.root / "notifications.json"
        self.write(report([make_item()]))
        self.client = FakeGitHub("ilyak0/privacy-ai-watch", "fake")
        self.gh = lambda repo, token: self.client

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, obj):
        self.rp.write_text(json.dumps(obj), encoding="utf-8")

    def run_notify(self, publish=True):
        with patch.dict("os.environ", {"GH_TOKEN": "fake"}):
            return notify.run(self.rp, self.sp, publish=publish, today=DAY,
                              github_factory=self.gh)

    def test_publish_once_and_rerun_does_not_repeat(self):
        first = self.run_notify()
        second = self.run_notify()
        self.assertEqual(first["url"], "https://github.com/o/r/issues/1")
        self.assertTrue(second["recovered"])
        self.assertEqual(self.client.posts, 1)
        state = json.loads(self.sp.read_text())
        self.assertTrue(state["items"][make_item()["canonical_url"]]["notified"])

    def test_lost_state_recovers_item_from_issue_markers(self):
        self.run_notify()
        self.sp.unlink()
        result = self.run_notify()
        self.assertTrue(result["recovered"])
        state = json.loads(self.sp.read_text())
        self.assertTrue(state["items"][make_item()["canonical_url"]]["notified"])

    def test_empty_day_is_quiet_and_dry_run_does_not_write_state(self):
        self.write(report())
        result = self.run_notify(publish=False)
        self.assertFalse(result["would_publish"])
        self.assertFalse(self.sp.exists())
        self.assertEqual(self.run_notify(), {"published": False, "reason": "no new items or source errors",
                                             "date": DAY.isoformat()})

    def test_first_run_baselines_old_archive(self):
        old = make_item(2, published="2026-09-01T00:00:00Z")
        self.write(report([make_item(), old]))
        self.run_notify()
        state = json.loads(self.sp.read_text())
        self.assertIn(old["canonical_url"], state["items"])
        self.assertFalse(state["items"][old["canonical_url"]]["notified"])

    def test_initialized_watch_notifies_day_eight_backfill(self):
        self.run_notify()
        backfill = make_item(8, published="2026-10-16T08:00:00Z")
        self.write(report([make_item(), backfill]))
        with patch.dict("os.environ", {"GH_TOKEN": "fake"}):
            result = notify.run(self.rp, self.sp, publish=True, today=dt.date(2026, 10, 24),
                                github_factory=self.gh)
        self.assertEqual(result["items"], 1)
        self.assertEqual(self.client.posts, 2)

    def test_unknown_publication_date_is_archive_only(self):
        undated = make_item(9, published=None)
        self.write(report([undated]))
        result = self.run_notify(publish=False)
        self.assertEqual(result["items"], 0)
        self.assertFalse(result["would_publish"])

    def test_hostile_title_cannot_mention_users_or_emit_html(self):
        hostile = make_item(title="Alert @someone <b>breaking</b> & now")
        body = notify.build_body(report([hostile]), [dict(hostile, _streams=[])], DAY)
        self.assertIn("@\u200bsomeone", body)
        self.assertIn("&lt;b&gt;", body)
        self.assertNotIn("<b>", body)
        self.assertEqual(body.count("@ilyak0"), 1)

    def test_cap_overflow_is_not_marked_notified(self):
        items = [make_item(i) for i in range(1, 4)]
        self.write(report(items))
        with patch.object(notify, "CAP_ITEMS", 1):
            self.run_notify()
        state = json.loads(self.sp.read_text())
        self.assertTrue(state["items"][items[2]["canonical_url"]]["notified"])
        self.assertNotIn(items[1]["canonical_url"], state["items"])

    def test_practice_precedes_newer_industry_and_events_and_ideas_are_separate(self):
        industry = make_item(2, 'Replit launches new AI coding model', '2026-10-17T07:00:00Z')
        event = make_item(3, 'Register for AI Act fines webinar', '2026-10-17T08:00:00Z')
        practice = make_item(1, 'CNIL fines company for cookie consent', '2026-10-16T08:00:00Z')
        r = report([industry, event, practice])
        rows, _ = notify.candidates(r, {}, DAY)
        self.assertEqual([i['canonical_url'] for i in rows], [practice['url'], industry['url'], event['url']])
        body = notify.build_body(r, rows, DAY)
        self.assertLess(body.index('### Юридическая практика'), body.index('### Контекст ИИ'))
        self.assertLess(body.index('### Контекст ИИ'), body.index('### События'))
        self.assertIn('### Вопросы для постов', body)
        self.assertIn('**LinkedIn**', body)
        self.assertIn('**Telegram**', body)
        self.assertIn('**X / Twitter**', body)
        self.assertEqual(body.count('<!-- item:'), 3)

    def test_api_failure_does_not_mark_items(self):
        class Broken(FakeGitHub):
            def create_issue(self, day, body):
                raise RuntimeError("network unavailable")
        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            with patch.dict("os.environ", {"GH_TOKEN": "fake"}):
                notify.run(self.rp, self.sp, publish=True, today=DAY, github_factory=Broken)
        self.assertFalse(self.sp.exists())

    def test_hostile_title_is_escaped_and_exact_url_kept(self):
        hostile = make_item(title="[click](javascript:alert(1)) `x` <script>")
        body = notify.build_body(report([hostile]), [dict(hostile, _streams=["privacy_world"])], DAY)
        self.assertNotIn("[click](javascript", body)
        self.assertIn(r"\[click\]", body)
        self.assertIn("https://example.test/story/1", body)


if __name__ == "__main__":
    unittest.main()
