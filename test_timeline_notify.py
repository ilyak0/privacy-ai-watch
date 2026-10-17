"""Offline notification tests for persistent story-timeline updates."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import notify
import timelines


DAY = dt.date(2026, 10, 17)
STAMP = "2026-10-16T08:00:00Z"
STORY_ID = "privacy-operations"
STORY_LABEL = "Практика обработки данных"


def event(url: str, title: str, published: str = STAMP, *,
          sources: list[dict] | None = None) -> dict:
    return {
        "id": hashlib.sha256(url.encode("utf-8")).hexdigest(),
        "canonical_url": url,
        "url": url,
        "title": title[:180] + ("…" if len(title) > 180 else ""),
        "published_utc": published,
        "first_seen_utc": "2026-10-16T09:00:00Z",
        "sources": sources or [{"label": "Официальный источник", "kind": "primary", "url": url}],
    }


def timeline_state(events: list[dict], baseline: list[str] | None = None) -> dict:
    return {
        "version": 1,
        "generated_at": "2026-10-17T08:00:00Z",
        "stories": [{
            "id": STORY_ID,
            "label": STORY_LABEL,
            "scope": "Тема практики обработки данных",
            "enabled": True,
            "initialized_at": "2026-10-16T09:00:00Z",
            "baseline_event_ids": baseline or [],
            "events": events,
        }],
    }


def item_for(url: str, title: str, published: str = STAMP) -> dict:
    return {
        "canonical_url": url,
        "url": url,
        "title": title,
        "published_utc": published,
        "first_seen_utc": "2026-10-17T00:00:00Z",
        "topics": ["privacy"],
        "regions": ["ru"],
        "source_ids": ["official"],
        "provenance": {"official": {"label": "Официальный источник", "kind": "primary",
                                      "observations": [{"url": url, "published_utc": published,
                                                        "topics": ["privacy"]}]}},
        "source_kind": "primary",
    }


def report(items=()) -> dict:
    buckets = {stream: [] for stream in notify.STREAMS}
    buckets["privacy_ru"] = list(items)
    return {"generated_at": "2026-10-17T08:00:00Z", "publication_window_days": 30,
            "buckets": buckets, "undated": [], "source_health": []}


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


class TimelineNotifyTests(unittest.TestCase):
    def test_dry_run_does_not_create_or_rewrite_delivery_state(self):
        fresh = event("https://example.test/current", "Публикация о практике обработки данных")
        self.write_timeline(timeline_state([fresh]))
        self.write_report([item_for(fresh["url"], fresh["title"])])
        result = notify.run(self.report_path, self.notify_path, publish=False, today=DAY,
                            timeline_path=self.timeline_path)
        self.assertEqual(result["timeline_updates"], 1)
        self.assertFalse(self.notify_path.exists())

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.report_path = self.root / "latest.json"
        self.notify_path = self.root / "notifications.json"
        self.timeline_path = self.root / "timelines.json"
        self.client = FakeGitHub("ilyak0/privacy-ai-watch", "fake")
        self.github_factory = lambda repo, token: self.client

    def tearDown(self):
        self.tmp.cleanup()

    def write_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_report(self, items=()) -> None:
        self.write_json(self.report_path, report(items))

    def write_timeline(self, value: dict) -> None:
        self.write_json(self.timeline_path, value)

    def run_notify(self, day=DAY):
        with patch.dict("os.environ", {"GH_TOKEN": "fake"}):
            return notify.run(self.report_path, self.notify_path, publish=True, today=day,
                              github_factory=self.github_factory, timeline_path=self.timeline_path)

    def test_first_creation_baselines_history_without_alerting(self):
        old = event("https://example.test/old", "Историческая публикация", "2026-09-01T08:00:00Z")
        state = timeline_state([old], baseline=[old["id"]])
        delivered = {}
        self.write_timeline(state)
        pending = timelines.pending_updates(state, delivered, DAY)
        self.assertEqual(pending, [])
        self.write_report()
        result = self.run_notify()
        self.assertFalse(result["published"])
        self.assertEqual(self.client.posts, 0)
        self.assertFalse(self.notify_path.exists())

    def test_new_story_event_is_rendered_once_and_not_repeated_in_bulk(self):
        old = event("https://example.test/old", "Историческая публикация", "2026-09-01T08:00:00Z")
        fresh = event("https://example.test/current", "Публикация о практике обработки данных")
        state = timeline_state([old, fresh], baseline=[old["id"]])
        self.write_timeline(state)
        row = item_for(fresh["url"], fresh["title"])
        self.write_report([row])

        result = self.run_notify()
        body = self.client.issues[DAY.isoformat()]["body"]
        delivery_key = hashlib.sha256(f"{STORY_ID}:{fresh['id']}".encode()).hexdigest()

        self.assertTrue(result["published"])
        self.assertIn("### Новое по сюжетам", body)
        self.assertIn(STORY_LABEL, body)
        self.assertEqual(body.count(fresh["url"]), 1)
        self.assertEqual(body.count(fresh["title"]), 1)
        self.assertEqual(body.count("<!-- timeline:"), 1)
        self.assertIn(f"<!-- timeline:{delivery_key} -->", body)
        self.assertEqual(body.count(f"<!-- item:{notify.item_marker(fresh['url'])} -->"), 1)
        self.assertNotIn(f"- **{fresh['title']}**", body.split("<details>", 1)[-1])

        ledger = json.loads(self.notify_path.read_text(encoding="utf-8"))
        self.assertIn(delivery_key, ledger["timeline_events"])
        self.assertEqual(ledger["timeline_events"][delivery_key]["story_id"], STORY_ID)

    def test_same_event_next_day_with_no_new_article_does_not_create_issue(self):
        fresh = event("https://example.test/current", "Публикация о практике обработки данных")
        self.write_timeline(timeline_state([fresh]))
        self.write_report([item_for(fresh["url"], fresh["title"])])
        self.run_notify()

        self.write_report()
        next_day = DAY + dt.timedelta(days=1)
        result = self.run_notify(next_day)
        self.assertFalse(result["published"])
        self.assertEqual(self.client.posts, 1)

    def test_lost_state_recovery_restores_item_and_timeline_markers(self):
        fresh = event("https://example.test/current", "Публикация о практике обработки данных")
        self.write_timeline(timeline_state([fresh]))
        row = item_for(fresh["url"], fresh["title"])
        self.write_report([row])
        self.run_notify()
        self.notify_path.unlink()

        recovered = self.run_notify()
        self.assertTrue(recovered["recovered"])
        self.assertEqual(self.client.posts, 1)
        ledger = json.loads(self.notify_path.read_text(encoding="utf-8"))
        delivery_key = hashlib.sha256(f"{STORY_ID}:{fresh['id']}".encode()).hexdigest()
        self.assertIn(delivery_key, ledger["timeline_events"])
        self.assertTrue(ledger["items"][fresh["url"]]["notified"])
        self.assertIn(delivery_key, ledger["days"][DAY.isoformat()]["timeline_markers"])

    def test_cap_leaves_unshown_timeline_event_pending(self):
        first = event("https://example.test/one", "Практика обработки данных: публикация A",
                      "2026-10-17T10:00:00Z")
        second = event("https://example.test/two", "Практика обработки данных: публикация B",
                       "2026-10-17T09:00:00Z")
        state = timeline_state([first, second])
        self.write_timeline(state)
        self.write_report([item_for(first["url"], first["title"], first["published_utc"]),
                           item_for(second["url"], second["title"], second["published_utc"])])

        with patch.object(notify, "CAP_ITEMS", 1):
            self.run_notify()
        body = self.client.issues[DAY.isoformat()]["body"]
        ledger = json.loads(self.notify_path.read_text(encoding="utf-8"))
        self.assertEqual(body.count("<!-- timeline:"), 1)
        self.assertEqual(len(ledger["timeline_events"]), 1)
        pending = timelines.pending_updates(state, ledger["timeline_events"], DAY)
        self.assertEqual(len(pending), 1)
        self.assertNotIn(pending[0]["delivery_key"], ledger["timeline_events"])

    def test_source_update_to_delivered_event_does_not_resend(self):
        fresh = event("https://example.test/current", "Публикация о практике обработки данных")
        state = timeline_state([fresh])
        self.write_timeline(state)
        row = item_for(fresh["url"], fresh["title"])
        self.write_report([row])
        self.run_notify()

        updated = dict(fresh, sources=[*fresh["sources"],
                                      {"label": "Поисковый индекс", "kind": "discovery",
                                       "url": "https://index.example/current"}])
        self.write_timeline(timeline_state([updated]))
        self.write_report([row])
        result = self.run_notify(DAY + dt.timedelta(days=1))
        self.assertFalse(result["published"])
        self.assertEqual(self.client.posts, 1)

    def test_issue_creation_failure_does_not_mark_timeline_event_delivered(self):
        fresh = event("https://example.test/current", "Публикация о практике обработки данных")
        self.write_timeline(timeline_state([fresh]))
        self.write_report([item_for(fresh["url"], fresh["title"])])

        class Broken(FakeGitHub):
            def create_issue(self, day, body):
                raise RuntimeError("network unavailable")

        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            with patch.dict("os.environ", {"GH_TOKEN": "fake"}):
                notify.run(self.report_path, self.notify_path, publish=True, today=DAY,
                           github_factory=Broken, timeline_path=self.timeline_path)
        self.assertFalse(self.notify_path.exists())


if __name__ == "__main__":
    unittest.main()
