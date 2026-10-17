import datetime as dt
import unittest

from briefing import select_briefing


AS_OF = dt.datetime(2026, 10, 17, 12, tzinfo=dt.timezone.utc)


def item(title, day=17, source="source-a", kind="primary", url=None):
    return {
        "title": title,
        "url": url or f"https://{source}.example/story/{day}/{abs(hash(title))}",
        "published_utc": f"2026-10-{day:02d}T09:00:00Z",
        "source_kind": kind,
        "source_ids": [source],
    }


class BriefingTests(unittest.TestCase):
    def test_ai_word_is_not_a_lawsuit_and_essay_does_not_enter_shortlist(self):
        rows = [item("Искусственный интеллект и будущее СМИ: что останется за человеком"),
                item("New essay: To keep human judgment, we need AI courts and AI-generated filings")]
        self.assertEqual(select_briefing(rows, as_of=AS_OF), [])

    def test_congress_announcement_and_generic_breach_mail_are_not_briefing(self):
        rows = [item("C 19 по 22 октября пройдёт Eurasian Data Protection Congress! Конгресс о приватности и регулировании ИИ"),
                item("Notice of Data Breach Dear Jane: school writes to inform you of an event")]
        self.assertEqual(select_briefing(rows, as_of=AS_OF), [])

    def test_recent_primary_practice_items_are_curated_and_bounded(self):
        items = [
            item("EDPB adopts new binding corporate rules opinion", source="edpb"),
            item("EDPB issues cross-border transfer guidance for BCRs", source="edpb", day=16),
            item("EDPB publishes opinion on international transfer rules", source="edpb", day=15),
            item("CNIL fines Acme for advertising cookies", source="cnil", day=16),
            item("Court orders DataCo to pay €2 million over biometric data", source="court", day=15),
            item("New bill on AI liability introduced in parliament", source="parliament", day=14),
        ]
        result = select_briefing(items, as_of=AS_OF)
        self.assertLessEqual(len(result), 5)
        self.assertLessEqual(sum(x["source_family"] == "edpb" for x in result), 2)
        self.assertLessEqual(sum(x["theme"] == "cross_border" for x in result), 2)
        self.assertTrue(any(x["theme"] == "enforcement" for x in result))
        self.assertTrue(all(x["basis"] == "headline" for x in result))
        self.assertTrue(all("что именно" not in x["why_open"].lower() or x["theme"] == "regulatory_initiative" for x in result))

    def test_same_xandr_story_collapses_and_prefers_primary(self):
        primary = {
            **item("EDPB fines Xandr for unlawful processing of personal data", source="edpb"),
            "provenance": {"edpb": {"kind": "primary", "label": "EDPB"}},
        }
        commentary = {
            **item("Xandr fined by EDPB over unlawful processing of personal data", source="privacy-news", kind="commentary", day=16),
            "provenance": {"privacy-news": {"kind": "commentary", "label": "Privacy News"}},
        }
        social = {
            **item("EDPB fines Xandr for unlawful processing of personal data", source="x-edpb", kind="social", day=16,
                   url="https://x.com/edpb/status/123"),
        }
        result = select_briefing([commentary, social, primary], as_of=AS_OF)
        xandr = [x for x in result if "Xandr" in x["title"]]
        self.assertEqual(len(xandr), 1)
        self.assertEqual(xandr[0]["url"], primary["url"])
        self.assertEqual(xandr[0]["source_kind"], "primary")

    def test_rounded_fine_rewrite_is_one_story(self):
        primary = item("Norwegian DPA fines Xandr EUR 8 600 000 for unlawful processing of personal data", source="edpb")
        rewrite = item("Privacy win: Norwegian Authority fines Microsoft’s Xandr € 8.5 Million", source="noyb", kind="commentary")
        result = select_briefing([rewrite, primary], as_of=AS_OF)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], primary["url"])

    def test_distinct_fines_are_not_merged(self):
        first = item("CNIL fines Acme €2 million for advertising cookies", source="cnil")
        second = item("CNIL fines Acme €5 million for biometric data processing", source="cnil", day=16)
        result = select_briefing([first, second], as_of=AS_OF)
        self.assertEqual({x["url"] for x in result}, {first["url"], second["url"]})

    def test_dates_urls_and_soft_headlines_are_excluded(self):
        inputs = [
            item("EDPB publishes cross-border BCR guidance", day=10),
            item("EDPB publishes cross-border BCR guidance", day=18),
            {**item("EDPB publishes cross-border BCR guidance"), "published_utc": None},
            item("EDPB publishes cross-border BCR guidance", url="javascript:alert(1)"),
            item("EDPB publishes cross-border BCR guidance", url="https://user:pass@example.com/x"),
            item("The state of privacy and digital governance: A conversation with Mike Hintze - IAPP"),
            item("Acme launches new AI model and raises $20 million"),
            item("Register for our AI Act and privacy webinar"),
        ]
        self.assertEqual(select_briefing(inputs, as_of=AS_OF), [])

    def test_sparse_pool_returns_only_qualified_items(self):
        good = item("New draft law proposes rules for AI liability", day=16)
        weak = item("Mike Hintze conversation about privacy", day=16, source="iapp")
        result = select_briefing([good, weak], as_of=AS_OF)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], good["url"])
        self.assertEqual(result[0]["theme"], "regulatory_initiative")

    def test_metadata_is_grounded_and_exact(self):
        source = {
            **item("Court orders DataCo to pay €2 million over biometric data", source="court"),
            "provenance": {
                "court": {"kind": "primary", "label": "Court"},
                "index": {"kind": "discovery", "label": "Search"},
            },
        }
        result = select_briefing([source], as_of=AS_OF)[0]
        self.assertEqual(result["title"], source["title"])
        self.assertEqual(result["source_kinds"], ["discovery", "primary"])
        self.assertIn("Сверить первичную публикацию", result["verification_needed"])
        self.assertNotIn("jurisdiction", result)
        self.assertNotIn("€2 million", result["why_open"])


if __name__ == "__main__":
    unittest.main()
