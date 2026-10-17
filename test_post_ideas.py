import unittest

from post_ideas import select_ideas


AS_OF = "2026-10-17T12:00:00Z"


def item(title, n=1, date="2026-10-16T12:00:00Z", kind="primary", **extra):
    result = {"title": title, "url": f"https://example.test/story/{n}",
              "published_utc": date, "source_kind": kind, "provenance": {}}
    result.update(extra)
    return result


class PostIdeasTests(unittest.TestCase):
    def test_explicit_headline_themes_create_channel_specific_questions(self):
        items = [item("Employer employee monitoring policy", n=1, date="2026-10-14T12:00:00Z"),
                 item("AI training data and copyright", n=2, date="2026-10-15T12:00:00Z"),
                 item("Cross-border transfer of personal data", n=3, date="2026-10-16T12:00:00Z")]
        result = select_ideas(items, as_of=AS_OF)
        self.assertEqual(len(result["linkedin"]), 3)
        self.assertEqual(len(result["telegram"]), 3)
        self.assertEqual(result["linkedin"][0]["theme"], "cross_border")
        self.assertNotEqual(result["linkedin"][0]["question"], result["telegram"][0]["question"])
        self.assertTrue(all(x["basis"] == "headline" for x in result["linkedin"] + result["telegram"]))

    def test_generic_ai_news_promotions_and_fine_tuning_are_excluded(self):
        items = [item("AI startup raises $40m", n=1),
                 item("Join our course on AI Act fines and 152-FZ", n=2),
                 item("Fine-tuning model released", n=3)]
        result = select_ideas(items, as_of=AS_OF)
        self.assertEqual(result, {"linkedin": [], "telegram": []})

    def test_undated_and_future_items_are_excluded(self):
        items = [item("Employee monitoring rules", date=None),
                 item("Employee monitoring rules", n=2, date="2026-10-18T00:00:00Z")]
        self.assertEqual(select_ideas(items, as_of=AS_OF)["linkedin"], [])

    def test_recent_window_is_anchored_to_as_of_not_oldest_archive(self):
        old = item("Cookie consent rules", date="2026-10-01T00:00:00Z")
        recent = item("Biometric data and privacy", n=2, date="2026-10-15T00:00:00Z")
        result = select_ideas([old, recent], as_of=AS_OF)
        self.assertEqual([x["theme"] for x in result["linkedin"]], ["biometrics"])

    def test_seven_day_cutoff_and_timezone_aware_as_of(self):
        in_range = item("Cookie consent rules", date="2026-10-10T12:00:00Z")
        out_range = item("Biometric data and privacy", n=2, date="2026-10-10T11:59:59Z")
        result = select_ideas([in_range, out_range], as_of="2026-10-17T12:00:00+00:00")
        self.assertEqual([x["theme"] for x in result["linkedin"]], ["consent_cookies"])

    def test_newest_item_per_theme_and_primary_tie_break(self):
        older = item("Cookie consent guidelines", n=1, date="2026-10-15T10:00:00Z", kind="primary")
        secondary = item("Cookie consent rules", n=2, date="2026-10-16T10:00:00Z", kind="search_index")
        primary = item("Cookie consent decision", n=3, date="2026-10-16T10:00:00Z", kind="primary")
        result = select_ideas([older, secondary, primary], as_of=AS_OF)
        self.assertEqual(len(result["linkedin"]), 1)
        self.assertEqual(result["linkedin"][0]["url"], primary["url"])

    def test_one_theme_per_channel_and_limit(self):
        items = [item("Cookie consent guidance", n=1), item("Cookie consent guidelines", n=2),
                 item("Biometric privacy", n=3), item("Employee monitoring policy", n=4)]
        result = select_ideas(items, limit_per_channel=2, as_of=AS_OF)
        for channel in ("linkedin", "telegram"):
            self.assertEqual(len(result[channel]), 2)
            self.assertEqual(len({x["theme"] for x in result[channel]}), len(result[channel]))

    def test_exact_title_url_date_and_source_kinds_are_retained(self):
        original = item("CNIL: Privacy cookies & consent", n=1, kind="primary",
                        provenance={"index": {"kind": "search_index"}})
        idea = select_ideas([original], as_of=AS_OF)["linkedin"][0]
        self.assertEqual(idea["title"], original["title"])
        self.assertEqual(idea["url"], original["url"])
        self.assertEqual(idea["published_utc"], original["published_utc"])
        self.assertEqual(idea["source_kinds"], ["primary", "search_index"])
        self.assertEqual(idea["source_kind"], "mixed")

    def test_urls_with_credentials_or_non_http_schemes_are_rejected(self):
        credential_url = item("Cookie consent rules", url="https://user:pass@example.test/story")
        javascript = item("Employee monitoring policy", n=2, url="javascript:alert(1)")
        result = select_ideas([credential_url, javascript], as_of=AS_OF)
        self.assertEqual(result, {"linkedin": [], "telegram": []})

    def test_title_triggers_are_strict_and_excerpt_cannot_supply_them(self):
        unrelated = item("A new platform launches", excerpt="Cookie consent rules and privacy choices")
        self.assertEqual(select_ideas([unrelated], as_of=AS_OF), {"linkedin": [], "telegram": []})

    def test_actual_archive_counterexamples_do_not_become_post_questions(self):
        rows = [item('Profiling web-edit with Python 3.15: flame graphs and source heatmaps'),
                item('NOTICE OF DATA BREACH - State of California - Department of Justice', n=2)]
        self.assertEqual(select_ideas(rows, as_of=AS_OF), {"linkedin": [], "telegram": []})


if __name__ == "__main__":
    unittest.main()
