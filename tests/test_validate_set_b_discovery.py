import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_set_b_discovery", ROOT / "scripts" / "validate_set_b_discovery.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def valid_manifest():
    return {
        "environment": "Codex",
        "request": "Find direct article URLs for the selected topic.",
        "returnedUrls": [{
            "url": "https://example.com/news/animation-ai",
            "sourceDomain": "example.com",
            "isDirectArticle": True,
            "title": "Animation AI report",
            "publicationEvidence": {
                "date": "2026-09-17",
                "evidence": "Search result publication date: 2026-09-17."
            },
            "geographyEvidence": {
                "status": "qualifying",
                "relationship": "United States",
                "evidence": "Search result identifies the development as US-based."
            }
        }]
    }


class SetBDiscoveryValidationTests(unittest.TestCase):
    def test_accepts_complete_direct_article_evidence(self):
        self.assertEqual([], MODULE.validate_manifest(valid_manifest()))

    def test_rejects_bare_url_without_geography_or_direct_article_evidence(self):
        manifest = valid_manifest()
        candidate = manifest["returnedUrls"][0]
        candidate.pop("geographyEvidence")
        candidate["isDirectArticle"] = False

        errors = MODULE.validate_manifest(manifest)

        self.assertTrue(any("geographyEvidence: is required" in error for error in errors))
        self.assertTrue(any("isDirectArticle: must be true" in error for error in errors))

    def test_allows_explicitly_nonqualifying_geography_for_pre_mapping_rejection(self):
        manifest = valid_manifest()
        manifest["returnedUrls"][0]["geographyEvidence"] = {
            "status": "not-established",
            "evidence": "The attributable search result contains no qualifying geography."
        }

        self.assertEqual([], MODULE.validate_manifest(manifest))
