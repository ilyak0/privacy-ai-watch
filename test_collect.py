import datetime as dt
import email.message
import gzip
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import collect


UTC = dt.timezone.utc
CONFIG = {"topics": {
    "privacy": [r"\bprivacy\b", r"\bdata protection\b"],
    "ai": [r"\bAI\b", r"\bartificial intelligence\b"],
}}


def source(transport="rss", **extra):
    result = {"id": "fixture", "label": "Fixture", "url": "https://example.test/feed",
              "transport": transport, "region": "world", "kind": "primary",
              "topics": ["privacy", "ai"], "enabled": True}
    result.update(extra)
    return result


class CollectorTests(unittest.TestCase):
    def test_paged_official_archive_uses_local_dates_and_stops_at_window(self):
        listing = source("html", url="http://example.test/news/", item_selector="article",
                         title_selector="a", link_selector="a", date_selector="time",
                         date_timezone="Europe/Moscow", page_url_template="http://example.test/news/page/{page}/")
        pages = [b'<article><a href="/news/new">Privacy rules</a><time datetime="2026-10-17 09:00:00"></time></article>',
                 b'<article><a href="/news/old">Privacy archive</a><time datetime="2026-09-16 09:00:00"></time></article>']
        with patch.object(collect, "fetch", side_effect=[(p, "text/html") for p in pages]) as request:
            rows, meta = collect.paged_html_items(listing, 30, dt.datetime(2026, 10, 17, 12, tzinfo=UTC))
        self.assertEqual(meta["pages_fetched"], 2)
        self.assertEqual(meta["window_coverage"], "boundary_reached")
        self.assertEqual(rows[0]["published"], dt.datetime(2026, 10, 17, 6, tzinfo=UTC))
        self.assertEqual(request.call_args_list[1].args[0], "http://example.test/news/page/2/")

    def test_pagination_caps_and_repeated_pages_are_visible(self):
        listing = source("html", item_selector="article", title_selector="a", link_selector="a",
                         date_selector="time", page_url_template="https://example.test/page/{page}", max_pages=1)
        data = b'<article><a href="/new">Privacy</a><time datetime="2026-10-17T06:00:00Z"></time></article>'
        now = dt.datetime(2026, 10, 17, 12, tzinfo=UTC)
        with patch.object(collect, "fetch", return_value=(data, "text/html")):
            _, capped = collect.paged_html_items(listing, 30, now)
            _, repeated = collect.paged_html_items({**listing, "max_pages": 4}, 30, now)
        self.assertEqual(capped["window_coverage"], "capped")
        self.assertEqual(repeated["window_coverage"], "repeated_page")
        self.assertEqual(repeated["pages_fetched"], 2)

    def test_feed_relative_links_resolve_to_publisher(self):
        data = b'<rss><channel><item><title>Privacy news</title><link>/en/news/one</link></item></channel></rss>'
        with patch.object(collect, 'fetch', return_value=(data, 'application/rss+xml')):
            _, rows = collect.process_source(source(), CONFIG, 30)
        self.assertEqual(rows[0]['url'], 'https://example.test/en/news/one')

    def test_gzip_feed_is_decompressed_before_xml_parsing(self):
        payload = b'<rss><channel><item><title>Privacy action</title><link>https://e.test/one</link></item></channel></rss>'
        response = io.BytesIO(gzip.compress(payload))
        response.headers = email.message.Message()
        response.headers['Content-Type'] = 'application/xml'
        response.headers['Content-Encoding'] = 'gzip'
        with patch.object(collect.urllib.request, 'urlopen', return_value=response):
            content, _ = collect.fetch('https://e.test/feed')
        self.assertEqual(collect.rss_items(content)[0]['title'], 'Privacy action')

    def test_report_does_not_invent_topic_region_combinations(self):
        stamp = collect.iso(collect.utcnow())
        item = {"canonical_url": "https://e.test/a", "url": "https://e.test/a",
                "title": "A shared story", "excerpt": "", "published_utc": stamp,
                "first_seen_utc": stamp, "topics": ["privacy", "ai"], "regions": ["world", "ru"],
                "provenance": {
                    "one": {"label": "One", "region": "world", "kind": "primary",
                            "observations": [{"topics": ["privacy"]}]},
                    "two": {"label": "Two", "region": "ru", "kind": "commentary",
                            "observations": [{"topics": ["ai"]}]}}}
        with tempfile.TemporaryDirectory() as folder:
            collect.write_reports(Path(folder), [item], [], [], 30, "fixture", {})
            report = json.loads((Path(folder) / "latest.json").read_text())
        self.assertEqual({k: len(v) for k, v in report["buckets"].items()},
                         {"privacy_world": 1, "privacy_ru": 0, "ai_world": 0, "ai_ru": 1})

    def test_undated_same_title_does_not_collapse_different_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            db = collect.init_db(Path(folder) / "archive.sqlite3")
            item = {"url": "https://e.test/one", "title": "Privacy roundup", "excerpt": "privacy",
                    "published": None, "topics": ["privacy"], "source_id": "one", "source_label": "One",
                    "region": "world", "source_kind": "primary", "first_seen": collect.iso(collect.utcnow())}
            collect.merge_items(db, [item, {**item, "url": "https://e.test/two"}])
            self.assertEqual(len(collect.read_undated(db)), 2)
            db.close()

    def test_atom_offset_parses_and_missing_date_stays_undated(self):
        data = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Privacy action</title><link href="https://e.test/1"/><updated>2026-10-17T12:00:00+03:00</updated><summary>Privacy enforcement.</summary></entry><entry><title>AI action</title><link href="https://e.test/2"/></entry></feed>'''
        rows = collect.rss_items(data)
        self.assertEqual(rows[0]["published"], dt.datetime(2026, 10, 17, 9, tzinfo=UTC))
        self.assertIsNone(rows[1]["published"])

    def test_valid_empty_rss_is_distinct_from_parse_error(self):
        with patch.object(collect, "fetch", return_value=(b"<rss><channel/></rss>", "application/rss+xml")):
            health, rows = collect.process_source(source(), CONFIG, 7)
        self.assertEqual(rows, [])
        self.assertEqual(health["status"], "valid_empty")
        with patch.object(collect, "fetch", return_value=(b"not xml", "application/rss+xml")):
            failed, _ = collect.process_source(source(), CONFIG, 7)
        self.assertEqual(failed["status"], "error")
        with patch.object(collect, "fetch", return_value=(b"<html><body>error page</body></html>", "text/html")):
            failed_page, _ = collect.process_source(source(), CONFIG, 7)
        self.assertEqual(failed_page["status"], "error")

    def test_html_200_without_configured_listing_is_explicit_failure(self):
        bad = source("html", item_selector="[data-article]")
        with patch.object(collect, "fetch", return_value=(b"<html><p>no listing</p></html>", "text/html")):
            health, rows = collect.process_source(bad, CONFIG, 7)
        self.assertEqual(rows, [])
        self.assertEqual(health["status"], "error")
        self.assertIn("matched no listing", health["error"])
        with patch.object(collect, "fetch", return_value=(b"<html></html>", "text/html")):
            health, _ = collect.process_source(source("html"), CONFIG, 7)
        self.assertEqual(health["status"], "error")
        self.assertIn("item_selector", health["error"])

    def test_html_exact_listing_extracts_date_and_relative_url(self):
        cfg = source("html", item_selector="article.story", title_selector="h2 a",
                     link_selector="h2 a", date_selector="time[datetime]",
                     excerpt_selector=".summary")
        data = b'<article class="story"><h2><a href="/law/1">AI privacy ruling</a></h2><time datetime="2026-10-17T10:00:00Z"></time><p class="summary">Artificial intelligence and privacy.</p></article>'
        with patch.object(collect, "fetch", return_value=(data, "text/html")), \
             patch.object(collect, "utcnow", return_value=dt.datetime(2026, 10, 17, 12, tzinfo=UTC)):
            health, rows = collect.process_source(cfg, CONFIG, 7)
        self.assertEqual(health["status"], "ok")
        self.assertEqual(rows[0]["url"], "https://example.test/law/1")
        self.assertEqual(rows[0]["topics"], ["privacy", "ai"])
        self.assertTrue(rows[0]["is_recent"])

    def test_telegram_profile_or_antibot_page_is_error(self):
        with patch.object(collect, "fetch", return_value=(b"<html><body>Channel preview</body></html>", "text/html")):
            health, rows = collect.process_source(source("telegram"), CONFIG, 7)
        self.assertEqual(rows, [])
        self.assertEqual(health["status"], "error")
        self.assertIn(".tgme_widget_message", health["error"])

    def test_keyword_regex_boundary_and_forced_topic(self):
        self.assertEqual(collect.topic_matches({"title": "AIs privacy", "excerpt": ""}, source(), CONFIG), ["privacy"])
        forced = source(force_topics=["ai"])
        self.assertEqual(collect.topic_matches({"title": "No matching words", "excerpt": ""}, forced, CONFIG), ["ai"])

    def test_configured_exclusions_do_not_treat_ai_token_as_crypto(self):
        config = {**CONFIG, "exclude_patterns": [r"\bcrypto\b", r"\bblockchain\b", r"\bNFT\b"]}
        self.assertEqual(collect.topic_matches({"title": "AI token governance", "excerpt": ""}, source(), config), ["ai"])
        self.assertEqual(collect.topic_matches({"title": "AI governance on blockchain", "excerpt": ""}, source(), config), [])

    def test_general_official_feeds_require_data_context_for_cross_border(self):
        config = collect.load_config(collect.DEFAULT_CONFIG)
        official = next(s for s in config['sources'] if s['id'] == 'duma-direct')
        railway = {"title": "Трансграничный участок железнодорожной колеи", "excerpt": ""}
        transfer = {"title": "Трансграничная передача персональных данных", "excerpt": ""}
        self.assertEqual(collect.topic_matches(railway, official, config), [])
        self.assertEqual(collect.topic_matches(transfer, official, config), ['privacy'])

    def test_optional_x_source_uses_exact_publication_date(self):
        row = {'title': 'Privacy guidance', 'excerpt': 'privacy',
               'url': 'https://x.com/official/status/123',
               'published': dt.datetime(2026, 10, 17, 10, tzinfo=UTC)}
        with patch('x_reader.fetch_posts', return_value=[row]), \
             patch.object(collect, 'utcnow', return_value=dt.datetime(2026, 10, 17, 12, tzinfo=UTC)):
            health, rows = collect.process_source(source('x_cookies'), CONFIG, 7)
        self.assertEqual(health['status'], 'ok')
        self.assertEqual(rows[0]['published'], row['published'])
        self.assertEqual(rows[0]['url'], row['url'])

    def test_canonical_url_and_exact_title_merge_provenance(self):
        self.assertEqual(collect.canonical_url("https://E.test/a?utm_source=x&x=1#frag"), "https://e.test/a?x=1")
        now = collect.iso(collect.utcnow())
        with tempfile.TemporaryDirectory() as folder:
            db = collect.init_db(Path(folder) / "archive.sqlite3")
            first = {"url": "https://e.test/story?utm_campaign=a", "title": "Privacy: a New Rule!",
                     "excerpt": "Exact excerpt", "published": collect.utcnow(), "topics": ["privacy"],
                     "source_id": "one", "source_label": "One", "region": "world", "source_kind": "primary", "jurisdictions": ["EU"], "first_seen": now}
            second = {**first, "url": "https://mirror.test/other", "source_id": "two", "source_label": "Two", "region": "ru", "source_kind": "commentary", "jurisdictions": ["RU"]}
            collect.merge_items(db, [first, second])
            rows = collect.read_recent(db, 7)
            self.assertEqual(len(rows), 1)
            self.assertEqual(set(rows[0]["provenance"]), {"one", "two"})
            self.assertEqual(rows[0]["source_ids"], ["one", "two"])
            self.assertEqual(rows[0]["regions"], ["ru", "world"])
            self.assertEqual(rows[0]["region"], "mixed")
            self.assertEqual(rows[0]["source_kind"], "mixed")
            self.assertEqual(rows[0]["provenance"]["one"]["observations"][0]["url"], first["url"])
            self.assertEqual(rows[0]["provenance"]["one"]["jurisdictions"], ["EU"])
            db.close()

    def test_exact_title_alias_does_not_merge_events_from_different_dates(self):
        with tempfile.TemporaryDirectory() as folder:
            db = collect.init_db(Path(folder) / "archive.sqlite3")
            now = collect.utcnow()
            first = {"url": "https://e.test/first", "title": "Privacy regulator issues decision",
                     "excerpt": "privacy", "published": now, "topics": ["privacy"],
                     "source_id": "one", "source_label": "One", "region": "world", "source_kind": "primary", "first_seen": collect.iso(now)}
            second = {**first, "url": "https://e.test/second", "published": now - dt.timedelta(days=7), "source_id": "two"}
            collect.merge_items(db, [first, second])
            count = db.execute("SELECT count(*) FROM items").fetchone()[0]
            self.assertEqual(count, 2)
            db.close()

    def test_old_and_future_dates_do_not_enter_recent_archive(self):
        now = collect.utcnow()
        old = {"title": "Privacy old", "url": "https://e.test/old", "excerpt": "privacy",
               "published": now - dt.timedelta(days=40)}
        future = {"title": "AI future", "url": "https://e.test/future", "excerpt": "AI",
                  "published": now + dt.timedelta(days=3)}
        xml_items = []
        for item in (old, future):
            date_text = item["published"].strftime("%a, %d %b %Y %H:%M:%S +0000")
            xml_items.append((f'<item><title>{item["title"]}</title><link>{item["url"]}</link>'
                              f'<description>{item["excerpt"]}</description><pubDate>{date_text}</pubDate></item>').encode())
        data = b'<rss><channel>' + b''.join(xml_items) + b'</channel></rss>'
        with patch.object(collect, "fetch", return_value=(data, "application/rss+xml")):
            health, rows = collect.process_source(source(), CONFIG, 7)
        self.assertEqual([r["title"] for r in rows], ["Privacy old"])
        self.assertFalse(rows[0]["is_recent"])
        self.assertEqual(health["recent"], 0)

    def test_archive_repeat_run_preserves_first_seen_and_undated(self):
        with tempfile.TemporaryDirectory() as folder:
            db = collect.init_db(Path(folder) / "archive.sqlite3")
            first_seen = collect.iso(collect.utcnow() - dt.timedelta(days=2))
            item = {"url": "https://e.test/undated", "title": "Privacy story", "excerpt": "privacy",
                    "published": None, "topics": ["privacy"], "source_id": "one", "source_label": "One",
                    "region": "ru", "source_kind": "primary", "first_seen": first_seen}
            collect.merge_items(db, [item]); collect.merge_items(db, [{**item, "first_seen": collect.iso(collect.utcnow()), "source_id": "two", "source_label": "Two"}])
            undated = collect.read_undated(db)
            self.assertEqual(len(undated), 1)
            self.assertEqual(undated[0]["first_seen_utc"], first_seen)
            self.assertEqual(set(undated[0]["provenance"]), {"one", "two"})
            db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
