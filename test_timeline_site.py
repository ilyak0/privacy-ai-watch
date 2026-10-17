import datetime as dt
import json
import tempfile
import unittest

import publish_site
import timelines


class TimelineSiteTests(unittest.TestCase):
    def test_independent_history_survives_empty_recent_report(self):
        stamp = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)
        catalog = {"version": 1, "stories": [{"id": "xandr", "label": "Xandr", "scope": "GDPR",
                   "all_of": [["Xandr"], ["GDPR"]], "exclude": []}]}
        item = {"title": "Xandr GDPR complaint", "url": "https://noyb.example/complaint",
                "published_utc": "2024-07-09T05:00:00Z", "excerpt": "PRIVATE EXCERPT"}
        state = timelines.update_timelines(None, [item], catalog, stamp)
        report = {"generated_at": "2026-10-17T12:00:00Z", "publication_window_days": 30, "buckets": {}}
        with tempfile.TemporaryDirectory() as path:
            _, data = publish_site.build_site(report, path, timeline_state=state)
            public = json.loads(data.read_text())
        self.assertEqual(public["unique_items"], 0)
        self.assertEqual(public["timelines"][0]["events"][0]["published_utc"], item["published_utc"])
        self.assertNotIn("PRIVATE EXCERPT", json.dumps(public))
        self.assertNotIn("baseline_event_ids", json.dumps(public))
        self.assertIn('id="timelines"', publish_site.PAGE)
        self.assertNotIn("innerHTML", publish_site.PAGE)


if __name__ == "__main__":
    unittest.main()
