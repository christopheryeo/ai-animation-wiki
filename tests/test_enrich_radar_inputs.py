import importlib.util
import pathlib
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "enrich_radar_inputs.py"
SPEC = importlib.util.spec_from_file_location("enrich_radar_inputs", SCRIPT)
ENRICH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENRICH)


class RadarInputEnrichmentTests(unittest.TestCase):
    def test_sentiment_prompt_contains_accepted_policy(self):
        self.assertIn("crashes, deaths, conflicts, or failures is Negative", ENRICH.SYSTEM_PROMPT)
        self.assertIn("Negative language appearing only inside quotations", ENRICH.SYSTEM_PROMPT)
        self.assertIn("Genuinely mixed positive and negative material is Neutral", ENRICH.SYSTEM_PROMPT)
        self.assertIn("full article body takes priority", ENRICH.SYSTEM_PROMPT)
        self.assertIn("Genuine ambiguity defaults to Neutral", ENRICH.SYSTEM_PROMPT)

    def test_tone_prompt_contains_accepted_policy(self):
        self.assertIn("question-based headline alone", ENRICH.SYSTEM_PROMPT)
        self.assertIn("publication-owned evaluations", ENRICH.SYSTEM_PROMPT)
        self.assertIn("Reporting why a share price moved is Factual", ENRICH.SYSTEM_PROMPT)
        self.assertIn("default to Factual", ENRICH.SYSTEM_PROMPT)

    def test_prompt_contains_accepted_metadata_and_event_policy(self):
        self.assertIn("reactive official briefing", ENRICH.SYSTEM_PROMPT)
        self.assertIn("default to Unfacilitated", ENRICH.SYSTEM_PROMPT)
        self.assertIn("Accept saved publisherName", ENRICH.SYSTEM_PROMPT)
        self.assertIn("unresolved outlet country may be empty", ENRICH.SYSTEM_PROMPT)
        self.assertIn("select Non-institutional", ENRICH.SYSTEM_PROMPT)

    def test_article_prompt_supplies_saved_publisher_evidence(self):
        metadata = {
            "articleTitle": "Example",
            "url": "https://example.sg/story",
            "publisherName": "Example News",
            "publisherDomain": "example.sg",
            "publisherLocation": "Southeast Asia",
            "publisherCountry": "Southeast Asia",
            "rawNewsApiResponse": (
                '{"source":{"title":"Example Wire","uri":"wire.example",'
                '"location":{"label":{"eng":"Southeast Asia"}}}}'
            ),
        }
        prompt = __import__("json").loads(
            ENRICH.article_prompt(metadata, "Body", "", "", [])
        )
        publisher = prompt["article"]["saved_publisher"]
        self.assertEqual(publisher["name"], "Example News")
        self.assertEqual(publisher["raw_source_name"], "Example Wire")
        self.assertEqual(publisher["raw_source_country"], "Southeast Asia")

    def test_frontmatter_parser_reads_current_input_shape(self):
        lines = [
            "articleId: '42'",
            "articleTitle: 'Writer''s argument'",
            "tags: ['AI Safety', 'OpenAI']",
            "outlets: []",
        ]
        parsed = ENRICH.parse_frontmatter(lines)
        self.assertEqual(parsed["articleId"], "42")
        self.assertEqual(parsed["articleTitle"], "Writer's argument")
        self.assertEqual(parsed["tags"], ["AI Safety", "OpenAI"])

    def test_shortlist_uses_existing_tag_surface_values(self):
        inventory = [
            ("Artificial Intelligence", "artificial intelligence", 20),
            ("Submarine", "submarine", 10),
        ]
        result = ENRICH.shortlist_tags("Artificial intelligence investment is rising.", inventory)
        self.assertEqual(result, ["Artificial Intelligence"])

    def test_discovery_includes_loose_and_month_inputs_by_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "loose.md").write_text("x", encoding="utf-8")
            (root / "2026-07").mkdir()
            (root / "2026-07" / "monthly.md").write_text("x", encoding="utf-8")
            (root / "not-a-month").mkdir()
            (root / "not-a-month" / "ignored.md").write_text("x", encoding="utf-8")
            self.assertEqual(len(ENRICH.discover_input_paths(root)), 2)
            self.assertEqual(
                [path.name for path in ENRICH.discover_input_paths(root, loose_only=True)],
                ["loose.md"],
            )

    def test_exact_path_manifest_does_not_select_same_named_loose_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            inputs = root / "Inputs" / "articles"
            (inputs / "2026-08").mkdir(parents=True)
            (inputs / "same.md").write_text("loose", encoding="utf-8")
            routed = inputs / "2026-08" / "same.md"
            routed.write_text("routed", encoding="utf-8")
            manifest = root / "manifest.txt"
            manifest.write_text("Inputs/articles/2026-08/same.md\n", encoding="utf-8")
            args = type("Args", (), {
                "input_dir": inputs,
                "loose_only": False,
                "article_id": None,
                "manifest": manifest,
                "limit": None,
            })()
            original_root = ENRICH.ROOT
            ENRICH.ROOT = root
            try:
                selected = ENRICH.select_input_paths(args)
            finally:
                ENRICH.ROOT = original_root
        self.assertEqual([path.resolve() for path in selected], [routed.resolve()])

    def test_frozen_loose_path_manifest_selects_uniquely_routed_article(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            inputs = root / "Inputs" / "articles"
            (inputs / "2026-08").mkdir(parents=True)
            routed = inputs / "2026-08" / "same.md"
            routed.write_text("routed", encoding="utf-8")
            manifest = root / "manifest.txt"
            manifest.write_text("Inputs/articles/same.md\n", encoding="utf-8")
            args = type("Args", (), {
                "input_dir": inputs,
                "loose_only": False,
                "article_id": None,
                "manifest": manifest,
                "limit": None,
            })()
            original_root = ENRICH.ROOT
            ENRICH.ROOT = root
            try:
                selected = ENRICH.select_input_paths(args)
            finally:
                ENRICH.ROOT = original_root
        self.assertEqual([path.resolve() for path in selected], [routed.resolve()])

    def test_consensus_requires_two_pass_agreement(self):
        primary = self.classification()
        review = self.classification()
        result = ENRICH.consensus(primary, review, 0.82, {"AI Safety"})
        self.assertTrue(result["autoApplicable"]["tone"])
        self.assertTrue(result["autoApplicable"]["eventType"])
        self.assertEqual(result["issueTags"], ["AI Safety"])

        review["tone"] = "Factual"
        result = ENRICH.consensus(primary, review, 0.82, {"AI Safety"})
        self.assertFalse(result["autoApplicable"]["tone"])
        self.assertIsNone(result["tone"])

    def test_apply_only_writes_registered_fields(self):
        lines = [
            "articleId: '42'",
            "category: 'Multilateral Relations'",
            "tone: 'Factual'",
            "eventType: 'Unfacilitated'",
            "tags: []",
            "outlets: []",
            "countries: []",
            "coverageCount: 1",
        ]
        result = {
            "tone": "Opinionated",
            "toneSentiment": "Negative",
            "eventType": "Facilitated",
            "issueTags": ["AI Safety"],
            "outletName": "Example News",
            "outletId": "canonical-example-news",
            "outletCountry": "Southeast Asia",
            "institutionalCategory": "National Security",
            "readyForCascade": True,
            "autoApplicable": {
                "tone": True, "toneSentiment": True, "eventType": True,
                "metadata": True, "tags": True,
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "article.md"
            path.write_text("---\n" + "\n".join(lines) + "\n---\n\nBody\n", encoding="utf-8")
            changed = ENRICH.apply_result(path, lines, "Body", result)
            text = path.read_text(encoding="utf-8")
        self.assertEqual(
            changed,
            [
                "category", "countries", "coverageCount", "eventType", "mediaCount",
                "outlets", "sourceType", "tags", "tone", "toneSentiment", "topic",
            ],
        )
        self.assertIn("tone: 'Opinionated'", text)
        self.assertIn("tags: ['AI Safety']", text)
        self.assertIn("outlets: ['canonical-example-news']", text)
        self.assertIn("coverageCount: 1", text)
        self.assertIn("mediaCount: 0", text)
        self.assertNotIn("coverageCount: '1'", text)
        self.assertNotIn("mediaCount: '0'", text)
        self.assertNotIn("confidence", text)

    def test_apply_assessment_deduplicates_articles_across_files(self):
        result = {
            "tone": "Opinionated",
            "toneSentiment": "Negative",
            "eventType": "Facilitated",
            "issueTags": ["AI Safety"],
            "outletName": "Example News",
            "outletCountry": "Southeast Asia",
            "institutionalCategory": "National Security",
            "readyForCascade": True,
            "autoApplicable": {
                "tone": True, "toneSentiment": True, "eventType": True,
                "metadata": True, "tags": True,
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            relative = pathlib.Path(folder) / "article.md"
            relative.write_text(
                "---\narticleId: '42'\ntone: 'Factual'\neventType: 'Unfacilitated'\n"
                "tags: []\noutlets: []\ncountries: []\ncategory: 'Other'\ncoverageCount: 1\n"
                "---\n\nBody\n",
                encoding="utf-8",
            )
            assessment = {
                "assessments": [{
                    "articleId": "42",
                    "path": str(relative),
                    "consensus": result,
                }]
            }
            one = pathlib.Path(folder) / "one.json"
            two = pathlib.Path(folder) / "two.json"
            one.write_text(__import__("json").dumps(assessment), encoding="utf-8")
            two.write_text(__import__("json").dumps(assessment), encoding="utf-8")
            original_root = ENRICH.ROOT
            ENRICH.ROOT = pathlib.Path("/")
            try:
                code = ENRICH.apply_assessments([one, two])
            finally:
                ENRICH.ROOT = original_root
        self.assertEqual(code, 0)

    def test_incomplete_consensus_is_held_without_writing(self):
        lines = ["articleId: '42'", "tone: 'Factual'"]
        result = {
            "tone": "Factual",
            "toneSentiment": None,
            "eventType": "Facilitated",
            "issueTags": [],
            "outletName": "Example News",
            "outletCountry": "Southeast Asia",
            "institutionalCategory": "Non-institutional",
            "readyForCascade": False,
            "autoApplicable": {
                "tone": True, "toneSentiment": False, "eventType": True,
                "metadata": True, "tags": False,
            },
        }
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "article.md"
            original = "---\n" + "\n".join(lines) + "\n---\n\nBody\n"
            path.write_text(original, encoding="utf-8")
            self.assertEqual(ENRICH.apply_result(path, lines, "Body", result), [])
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_complete_input_gate_accepts_normalized_crawler(self):
        note = (
            "---\narticleId: '42'\narticleTitle: 'Title'\npublishedDate: '2026-08-10T00:00:00Z'\n"
            "category: 'Non-institutional'\ntopic: 'Non-institutional'\ntone: 'Factual'\n"
            "toneSentiment: 'Neutral'\neventType: 'Facilitated'\ntags: []\n"
            "outlets: ['example']\ncountries: []\ncoverageCount: 1\nmediaCount: 0\n"
            "sourceType: 'crawl'\nurl: 'https://example.com/story'\n---\n\nBody\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "article.md"
            path.write_text(note, encoding="utf-8")
            original_root = ENRICH.ROOT
            ENRICH.ROOT = pathlib.Path(folder)
            try:
                code = ENRICH.check_complete_inputs([path])
            finally:
                ENRICH.ROOT = original_root
        self.assertEqual(code, 0)

    def test_process_result_preserves_relevance_and_duplicate_signals(self):
        note = (
            "---\narticleId: '42'\narticleTitle: 'Title'\nurl: 'https://example.com'\n"
            "relevant: 'false'\nrelevance_confidence: '0.2'\nrelevance_reason: 'Weak match'\n"
            "duplicateFlag: 'true'\nduplicateList: '[41]'\n---\n\nBody\n"
        )
        classification = self.classification()
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "article.md"
            path.write_text(note, encoding="utf-8")
            original_root = ENRICH.ROOT
            ENRICH.ROOT = pathlib.Path(folder)
            original_call = ENRICH.call_model
            ENRICH.call_model = lambda *args, **kwargs: classification.copy()
            try:
                args = type("Args", (), {"no_fetch": True, "fetch_timeout": 1, "model": "test", "api_timeout": 1, "confidence": 0.82, "apply": False})()
                result = ENRICH.process_one(path, [], "key", args)
            finally:
                ENRICH.call_model = original_call
                ENRICH.ROOT = original_root
        self.assertEqual(result["inputSignals"]["relevant"], "false")
        self.assertEqual(result["inputSignals"]["duplicateFlag"], "true")

    @staticmethod
    def classification():
        return {
            "tone": "Opinionated",
            "tone_confidence": 0.93,
            "tone_evidence": ["The author argues"],
            "tone_sentiment": "Negative",
            "sentiment_confidence": 0.92,
            "sentiment_evidence": ["The report warns"],
            "event_type": "Facilitated",
            "event_confidence": 0.91,
            "event_trigger": "scheduled report",
            "event_evidence": ["The report was released"],
            "issue_tags": ["AI Safety"],
            "outlet_name": "Example News",
            "outlet_country": "Southeast Asia",
            "institutional_category": "National Security",
            "metadata_confidence": 0.90,
            "review_required": False,
            "review_reason": "",
        }


if __name__ == "__main__":
    unittest.main()
