import unittest

from editorial import classify, sort_key


class EditorialTests(unittest.TestCase):
    def test_non_english_official_privacy_metadata_is_practice(self):
        item = {'title': 'Reconstitution d’adresses électroniques professionnelles',
                'source_kind': 'primary', 'topics': ['privacy']}
        self.assertEqual(classify(item)['section'], 'practice')

    def test_practice_signals(self):
        cases = [
            ("EDPB fines Xandr over personal data", "enforcement_decision"),
            ("В Госдуму внесен законопроект об ИИ", "regulation"),
            ("CNIL publishes privacy guidelines", "privacy_guidance"),
            ("Против Nvidia подан иск из-за авторских прав", "enforcement_decision"),
        ]
        for title, evidence in cases:
            with self.subTest(title=title):
                result = classify({"title": title})
                self.assertEqual(result["section"], "practice")
                self.assertIn(f"lexical:{evidence}", result["evidence"])

    def test_primary_source_does_not_make_general_ai_research_practice(self):
        result = classify({"title": "NIST publishes new AI benchmark study",
                           "source_kind": "primary"})
        self.assertEqual(result["section"], "ai_context")
        self.assertIn("source_kind:primary", result["evidence"])

    def test_tools_and_industry_news_are_context(self):
        for title in ("New legal tools launch", "Replit and Anthropic announce product news"):
            with self.subTest(title=title):
                self.assertEqual(classify({"title": title})["section"], "ai_context")

    def test_legal_training_promotion_is_event(self):
        title = "Юристы, приглашаем на курс по AI Act, штрафам и 152-ФЗ"
        result = classify({"title": title})
        self.assertEqual(result["section"], "events")
        self.assertEqual(result["label"], "События и обучение")

    def test_recording_with_actual_new_law_is_practice(self):
        result = classify({"title": "Запись вебинара: закон об ИИ принят парламентом"})
        self.assertEqual(result["section"], "practice")
        self.assertIn("lexical:substantive_action", result["evidence"])

    def test_funding_and_fine_tuning_are_not_fines(self):
        for title in ("AI startup raises $40m in funding", "New fine-tuning model released"):
            with self.subTest(title=title):
                result = classify({"title": title})
                self.assertEqual(result["section"], "ai_context")
                self.assertNotIn("lexical:enforcement_decision", result["evidence"])
        self.assertEqual(classify({"title": "Join our fine-tuning webinar"})["section"], "events")

    def test_provenance_kinds_are_preserved_as_evidence(self):
        result = classify({"title": "CNIL publishes privacy guidelines",
                           "provenance": {"src": {"kind": "primary"},
                                          "index": {"kind": "search_index"}}})
        self.assertIn("source_kind:primary", result["evidence"])
        self.assertIn("source_kind:search_index", result["evidence"])

    def test_sort_priority_and_publication_time(self):
        practice = {"title": "EDPB fines Xandr", "published_utc": "2026-01-01T00:00:00Z"}
        ai = {"title": "AI model released", "published_utc": "2026-10-01T00:00:00Z"}
        events = {"title": "Join our AI webinar"}
        ordered = sorted([events, ai, practice], key=sort_key, reverse=True)
        self.assertEqual(ordered, [practice, ai, events])
        self.assertGreater(classify(practice)["score"], classify(ai)["score"])


if __name__ == "__main__":
    unittest.main()
