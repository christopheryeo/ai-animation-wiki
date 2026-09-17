import importlib.util
import inspect
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CASCADE = load_script("ingest_cascade")


class IngestCascadeInputGateTests(unittest.TestCase):
    def note(self, **overrides):
        data = {
            "articleId": "crawl-1",
            "articleTitle": "Example",
            "publishedDate": "2026-08-15T01:00:00Z",
            "category": "Non-institutional",
            "topic": "Air Capabilities",
            "tone": "Factual",
            "toneSentiment": "Neutral",
            "eventType": "Unfacilitated",
            "tags": ["Readiness"],
            "outlets": ["Example News"],
            "countries": [],
            "coverageCount": 1,
            "mediaCount": 0,
            "sourceType": "crawl",
            "url": "https://example.test/article",
        }
        data.update(overrides)
        frontmatter = CASCADE.yaml.safe_dump(data, sort_keys=False).strip()
        return f"---\n{frontmatter}\n---\n\nSaved source body.\n"

    def validate(self, text):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = pathlib.Path(folder) / "article.md"
            path.write_text(text, encoding="utf-8")
            return CASCADE.validate_cascade_inputs([path])

    def test_complete_policy_approved_input_passes(self):
        self.assertEqual(self.validate(self.note()), [])

    def test_unknown_outlet_country_may_remain_empty(self):
        self.assertEqual(self.validate(self.note(countries=[])), [])

    def test_missing_sentiment_is_rejected(self):
        findings = self.validate(self.note(toneSentiment=None))
        self.assertTrue(any(
            item["field"] == "toneSentiment" and item["issue"] == "invalid value"
            for item in findings
        ))

    def test_unregistered_category_is_rejected(self):
        findings = self.validate(self.note(category="Invented Category"))
        self.assertTrue(any(
            item["field"] == "category" and item["issue"] == "invalid value"
            for item in findings
        ))

    def test_batch_gate_raises_before_cascade(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            good = pathlib.Path(folder) / "good.md"
            bad = pathlib.Path(folder) / "bad.md"
            good.write_text(self.note(), encoding="utf-8")
            bad.write_text(self.note(eventType="Unknown"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cascade-ready input gate failed"):
                CASCADE.require_cascade_ready_inputs([good, bad])

    def test_cascade_runner_is_local_only(self):
        self.assertFalse(hasattr(CASCADE, "prepare_verified_uat_delta"))
        source = inspect.getsource(CASCADE.run_batch)
        self.assertNotIn("project_wiki_to_uat", source)
        self.assertNotIn("uat_delta_prepare_verify", source)


if __name__ == "__main__":
    unittest.main()
