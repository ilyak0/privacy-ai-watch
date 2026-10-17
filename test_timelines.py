import copy
import datetime as dt
import hashlib
import unittest
from zoneinfo import ZoneInfo

import timelines


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 17, 12, tzinfo=UTC)
CATALOG = {"version": 1, "stories": [
    {"id": "ross_case", "label": "Thomson Reuters v ROSS", "scope": "US",
     "all_of": [["Thomson Reuters"], ["ROSS"], ["copyright|3rd Circuit"]], "exclude": []},
    {"id": "xandr_case", "label": "Xandr Norway", "scope": "Norway",
     "all_of": [["Xandr"], ["Norwegian DPA|Norwegian Authority"], ["fines|fine"]], "exclude": []},
]}


def item(title, url, published="2026-10-17T08:00:00Z", source="feed", kind="commentary"):
    return {"title": title, "url": url, "canonical_url": url, "published_utc": published,
            "first_seen_utc": "2026-10-17T08:05:00Z", "excerpt": "PRIVATE EXCERPT",
            "source_ids": [source], "provenance": {source: {"label": source, "kind": kind,
                "observations": [{"url": url, "published_utc": published}]}}}


class TimelineTests(unittest.TestCase):
    def test_renamed_known_url_keeps_story_identity_and_adds_source(self):
        prior = timelines.update_timelines(None, [item("Thomson Reuters ROSS copyright", "https://news.example/known")], CATALOG, NOW)
        changed = item("Headline corrected without the original case name", "https://news.example/known", source="official", kind="primary")
        state = timelines.update_timelines(prior, [changed], CATALOG, NOW)
        events = state["stories"][0]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["sources"]), 2)

    def test_future_stored_record_is_not_rendered_and_invalid_link_cannot_inherit_primary(self):
        row = item("Thomson Reuters ROSS copyright", "https://news.example/one")
        row["provenance"]["feed"]["observations"][0]["url"] = "https://user:pass@example.test/one"
        state = timelines.update_timelines(None, [row], CATALOG, NOW)
        self.assertEqual(state["stories"][0]["events"][0]["sources"], [])
        state["stories"][0]["events"][0]["published_utc"] = "2026-10-18T10:00:00Z"
        public = timelines.public_timelines(state, as_of=NOW)
        self.assertEqual(sum(s["publication_count"] for s in public), 0)

    def test_first_creation_baselines_existing_records_even_if_empty(self):
        state = timelines.update_timelines(None, [item(
            "3rd Circuit Rules For Thomson Reuters in ROSS copyright fight",
            "https://news.example/ross")], CATALOG, NOW)
        story = state["stories"][0]
        self.assertEqual(story["baseline_event_ids"], [story["events"][0]["id"]])
        self.assertEqual(timelines.pending_updates(state, {}, "2026-10-17"), [])
        empty = timelines.update_timelines(None, [], CATALOG, NOW)
        later = timelines.update_timelines(empty, [item(
            "3rd Circuit Rules For Thomson Reuters in ROSS copyright fight",
            "https://news.example/ross")], CATALOG, NOW)
        self.assertEqual(later["stories"][0]["baseline_event_ids"], [])
        self.assertEqual(len(timelines.pending_updates(later, {}, "2026-10-17")), 1)

    def test_known_canonical_url_merges_sources_without_new_event_or_date_change(self):
        old = item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://news.example/ross",
                   "2026-10-16T08:00:00Z")
        prior = timelines.update_timelines(None, [old], CATALOG, NOW)
        new = copy.deepcopy(old)
        new["url"] = "https://news.example/ross?utm_source=feed"
        new["canonical_url"] = "https://news.example/ross"
        new["published_utc"] = "2026-10-17T08:00:00Z"
        new["source_ids"] = ["official"]
        new["provenance"] = {"official": {"label": "official", "kind": "primary",
            "observations": [{"url": new["url"], "published_utc": new["published_utc"]}]}}
        updated = timelines.update_timelines(prior, [new], CATALOG, NOW)
        events = updated["stories"][0]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["published_utc"], "2026-10-16T08:00:00Z")
        self.assertEqual({s["kind"] for s in events[0]["sources"]}, {"commentary", "primary"})

    def test_distinct_case_events_are_not_semantically_merged(self):
        items = [
            item("Norwegian DPA fines Xandr for unlawful data processing", "https://a.example/1"),
            item("Norwegian Authority fines Microsoft Xandr", "https://a.example/2"),
            item("3rd Circuit Rules For Thomson Reuters in ROSS copyright fight", "https://b.example/1"),
            item("ROSS asks Supreme Court review in Thomson Reuters copyright case", "https://b.example/2"),
        ]
        state = timelines.update_timelines(None, items, CATALOG, NOW)
        by_id = {s["id"]: s for s in state["stories"]}
        self.assertEqual(len(by_id["xandr_case"]["events"]), 2)
        self.assertEqual(len(by_id["ross_case"]["events"]), 2)

    def test_removed_story_is_disabled_but_history_retained(self):
        prior = timelines.update_timelines(None, [item(
            "3rd Circuit Rules For Thomson Reuters in ROSS copyright fight",
            "https://news.example/ross")], CATALOG, NOW)
        reduced = {"version": 1, "stories": [CATALOG["stories"][1]]}
        updated = timelines.update_timelines(prior, [], reduced, NOW)
        retired = next(s for s in updated["stories"] if s["id"] == "ross_case")
        self.assertEqual(len(retired["events"]), 1)
        self.assertFalse(retired["enabled"])
        self.assertNotIn("ross_case", [s["id"] for s in timelines.public_timelines(updated)])

    def test_invalid_urls_dates_future_and_private_strings_never_publish(self):
        candidates = [
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "javascript:alert(1)"),
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://user:pass@news.example/a"),
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://news.example/a?email=alice@example.test"),
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://news.example/naive", "2026-10-17T08:00:00"),
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://news.example/future", "2026-10-18T08:00:00Z"),
            item("3rd Circuit Rules Thomson Reuters ROSS copyright alice@example.test /Users/alice/private",
                 "https://news.example/safe"),
        ]
        state = timelines.update_timelines(None, candidates, CATALOG, NOW)
        events = state["stories"][0]["events"]
        self.assertEqual(len(events), 1)
        public = timelines.public_timelines(state)
        rendered = repr(public)
        self.assertNotIn("alice@example.test", rendered)
        self.assertNotIn("/Users/", rendered)
        self.assertNotIn("PRIVATE EXCERPT", rendered)

    def test_public_timeline_has_age_history_and_safe_source_kinds(self):
        state = timelines.update_timelines(None, [item(
            "3rd Circuit Rules For Thomson Reuters in ROSS copyright fight",
            "https://news.example/ross", kind="discovery")], CATALOG, NOW)
        story = timelines.public_timelines(state, as_of=NOW)[0]
        self.assertEqual(story["publication_count"], 1)
        self.assertEqual(story["history_status"], "watching")
        self.assertFalse(story["stale"])
        self.assertEqual(story["events"][0]["sources"][0]["kind"], "discovery")
        self.assertNotIn("excerpt", story["events"][0])

    def test_pending_updates_use_moscow_day_delivery_key_and_age_window(self):
        items = [
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://news.example/today",
                 "2026-10-17T01:00:00Z"),
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://news.example/old",
                 "2026-09-16T01:00:00Z"),
            item("3rd Circuit Rules Thomson Reuters ROSS copyright", "https://news.example/future",
                 "2026-10-18T01:00:00Z"),
        ]
        initial = timelines.update_timelines(None, [], CATALOG, NOW)
        state = timelines.update_timelines(initial, items, CATALOG, NOW)
        pending = timelines.pending_updates(state, {}, "2026-10-17", window_days=30)
        self.assertEqual([x["url"] for x in pending], ["https://news.example/today"])
        expected = hashlib.sha256(("ross_case:" + pending[0]["id"]).encode()).hexdigest()
        self.assertEqual(pending[0]["delivery_key"], expected)
        self.assertEqual(timelines.pending_updates(state, {expected: True}, "2026-10-17"), [])

    def test_bad_catalog_rejected_before_state_is_changed(self):
        prior = timelines.update_timelines(None, [], CATALOG, NOW)
        bad = {"version": 1, "stories": [{"id": "bad", "label": "bad", "scope": "x",
                "all_of": [["["]], "exclude": []}]}
        original = copy.deepcopy(prior)
        with self.assertRaises(ValueError):
            timelines.update_timelines(prior, [], bad, NOW)
        self.assertEqual(prior, original)


if __name__ == "__main__":
    unittest.main()
