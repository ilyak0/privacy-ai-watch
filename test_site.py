import json
import tempfile
import unittest
from pathlib import Path

import publish_site


def item(url, title, sources, published="2026-10-17T10:00:00Z", excerpt="SECRET EXCERPT"):
    return {"url": url, "canonical_url": url.split("?", 1)[0], "title": title,
            "published_utc": published, "excerpt": excerpt, "provenance": sources}


class PublishSiteTests(unittest.TestCase):
    def report(self):
        shared = item("https://news.example/a?utm_source=x", '<img src=x onerror="alert(1)"> user@example.com /Users/alice/private', {
            "s1": {"label": "Official", "region": "world", "kind": "primary", "observations": [
                {"url": "https://news.example/a?utm_source=x", "published_utc": "2026-10-17T10:00:00Z", "topics": ["privacy"]}]},
            "s2": {"label": "Russian source", "region": "ru", "kind": "discovery", "observations": [
                {"url": "https://index.example/search?q=a", "published_utc": "2026-10-17T10:00:00Z", "topics": ["ai"]}]}})
        dup = dict(shared, url="https://news.example/a?utm_medium=y")
        return {"generated_at": "2026-10-17T12:00:00Z", "publication_window_days": 7,
                "buckets": {"privacy_world": [shared], "ai_ru": [dup]}, "undated": [],
                "source_health": [{"label": "Official", "region": "world", "kind": "primary", "status": "ok",
                    "parsed": 1, "recent": 1, "undated": 0, "checked_at": "2026-10-17T12:00:00Z",
                    "latest_publication": "2026-10-17T10:00:00Z", "error": "private /Users/alice/token"}]}

    def test_dedup_memberships_are_observation_scoped_and_public(self):
        out = publish_site.public_report(self.report())
        self.assertEqual(out["unique_items"], 1)
        record = out["items"][0]
        self.assertEqual(set(record["memberships"]), {"privacy_world", "ai_ru"})
        self.assertNotIn("privacy_ru", record["memberships"])
        self.assertNotIn("ai_world", record["memberships"])
        self.assertEqual(out["membership_counts"]["privacy_world"], 1)
        self.assertEqual(out["membership_counts"]["ai_ru"], 1)
        self.assertEqual(len(record["sources"]), 2)

    def test_untrusted_content_escaped_and_private_material_removed(self):
        out = publish_site.public_report(self.report())
        encoded = json.dumps(out, ensure_ascii=False)
        self.assertNotIn("user@example.com", encoded)
        self.assertNotIn("/Users/alice", encoded)
        self.assertNotIn("SECRET EXCERPT", encoded)
        self.assertNotIn("private /Users/alice/token", encoded)
        with tempfile.TemporaryDirectory() as d:
            page, data = publish_site.build_site(self.report(), d)
            self.assertEqual(page.name, "index.html")
            self.assertIn("textContent", page.read_text())
            self.assertNotIn("innerHTML", page.read_text())
            self.assertNotIn("<img", data.read_text())
            self.assertIn("\\u003cimg", data.read_text())

    def test_all_filters_and_source_kinds_are_present(self):
        page = publish_site.PAGE
        for key in ("privacy_world", "privacy_ru", "ai_world", "ai_ru", "undated"):
            self.assertIn(f'data-filter="{key}"', page)
        for kind in ("primary", "commentary", "discovery"):
            self.assertIn(kind, page)
        self.assertIn("поток источника", page)

    def test_dates_are_sanitized_and_malformed_counts_safe(self):
        report = self.report()
        report["generated_at"] = "<script>"
        report["publication_window_days"] = "nope"
        out = publish_site.public_report(report)
        self.assertIsNone(out["generated_at"])
        self.assertEqual(out["publication_window_days"], 0)

    def test_practice_order_and_separate_post_ideas_preserve_source_labels(self):
        r = self.report()
        practice = item('https://example.test/cookies', 'CNIL fines company over cookie consent', {})
        practice['source_kind'] = 'primary'
        industry = item('https://example.test/model', 'New AI coding model launches', {}, '2026-10-17T11:00:00Z')
        r['buckets'] = {'privacy_world': [industry, practice]}
        out = publish_site.public_report(r)
        self.assertEqual(out['items'][0]['url'], practice['url'])
        self.assertEqual(out['items'][0]['editorial']['section'], 'practice')
        self.assertEqual(len(out['post_ideas']['linkedin']), 1)
        self.assertEqual(len(out['post_ideas']['x']), 1)
        self.assertEqual(out['post_ideas']['linkedin'][0]['url'], practice['url'])
        self.assertIn('id="post-ideas"', publish_site.PAGE)
        self.assertIn('id="briefing"', publish_site.PAGE)
        self.assertEqual(out['briefing'][0]['url'], practice['url'])
        self.assertEqual(out['briefing'][0]['basis'], 'headline')
        self.assertIn('id="section"', publish_site.PAGE)


if __name__ == "__main__":
    unittest.main()
