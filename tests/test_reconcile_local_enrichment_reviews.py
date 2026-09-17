import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SCRIPT = SCRIPT_DIR / "reconcile_local_enrichment_reviews.py"
SPEC = importlib.util.spec_from_file_location("reconcile_local_enrichment_reviews", SCRIPT)
RECONCILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECONCILE)


class LocalReviewReconciliationTests(unittest.TestCase):
    def test_sentiment_adjudication_removes_only_sentiment_hold(self):
        merged = {
            "toneSentiment": None,
            "sentimentConfidence": 0.7,
            "sentimentEvidence": [],
            "autoApplicable": {"toneSentiment": False},
            "reviewReasons": [
                "metadata disagreement or low confidence",
                "tone-sentiment disagreement or low confidence",
            ],
        }
        decision = {
            "toneSentiment": "Neutral",
            "confidence": 0.91,
            "evidence": "The report described both outcomes.",
            "rationale": "Rule 3: mixed material is Neutral.",
        }
        RECONCILE.apply_sentiment_adjudication("42", merged, decision)
        self.assertEqual(merged["toneSentiment"], "Neutral")
        self.assertTrue(merged["autoApplicable"]["toneSentiment"])
        self.assertEqual(merged["reviewReasons"], ["metadata disagreement or low confidence"])

    def test_sentiment_adjudication_file_requires_complete_valid_counts(self):
        payload = {
            "policyVersion": "test-policy",
            "inputCount": 1,
            "adjudicatedCount": 1,
            "decisions": [{
                "articleId": "42",
                "path": "Inputs/articles/2026-08/article.md",
                "toneSentiment": "Negative",
                "confidence": 0.9,
                "evidence": "The aircraft crashed.",
                "rationale": "Rule 1: factual crash reporting is Negative.",
            }],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "adjudication.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            indexed, policy = RECONCILE.index_sentiment_adjudication(path)
        self.assertEqual(policy, "test-policy")
        self.assertEqual(indexed["42"]["toneSentiment"], "Negative")

    def test_tone_adjudication_clears_only_tone_hold(self):
        merged = {
            "tone": None,
            "toneConfidence": 0.7,
            "toneEvidence": [],
            "autoApplicable": {"tone": False},
            "reviewReasons": [
                "tone disagreement or low confidence",
                "event-type disagreement or low confidence",
            ],
        }
        decision = {
            "tone": "Factual",
            "confidence": 0.92,
            "evidence": "Shares climbed after the company announced the agreement.",
            "rationale": "The body explains the price movement without recommending the stock.",
        }
        RECONCILE.apply_tone_adjudication("42", merged, decision)
        self.assertEqual(merged["tone"], "Factual")
        self.assertTrue(merged["autoApplicable"]["tone"])
        self.assertEqual(merged["reviewReasons"], ["event-type disagreement or low confidence"])

    def test_tone_adjudication_file_requires_complete_valid_counts(self):
        payload = {
            "policyVersion": "test-tone-policy",
            "inputCount": 1,
            "adjudicatedCount": 1,
            "decisions": [{
                "articleId": "42",
                "path": "Inputs/articles/2026-08/article.md",
                "tone": "Opinionated",
                "confidence": 0.9,
                "evidence": "The government must act immediately.",
                "rationale": "The publication advances its own recommendation.",
            }],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "tone-adjudication.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            indexed, policy = RECONCILE.index_tone_adjudication(path)
        self.assertEqual(policy, "test-tone-policy")
        self.assertEqual(indexed["42"]["tone"], "Opinionated")

    def test_metadata_adjudication_allows_blank_country_and_clears_only_metadata(self):
        merged = {
            "outletName": None,
            "outletCountry": None,
            "institutionalCategory": None,
            "metadataConfidence": 0.6,
            "autoApplicable": {"metadata": False},
            "reviewReasons": [
                "metadata disagreement or low confidence",
                "tone disagreement or low confidence",
            ],
        }
        decision = {
            "outletName": "Example News",
            "outletId": "example-news",
            "outletCountry": "",
            "institutionalCategory": "Non-institutional",
            "confidence": 0.9,
            "evidence": "Published by Example News",
            "sourceBasis": "saved publisherName",
            "originalAgency": "Reuters",
            "rationale": "Publishing website is the outlet; country is unresolved.",
        }
        RECONCILE.apply_metadata_adjudication("42", merged, decision)
        self.assertEqual(merged["outletCountry"], "")
        self.assertEqual(merged["outletId"], "example-news")
        self.assertTrue(merged["autoApplicable"]["metadata"])
        self.assertEqual(merged["reviewReasons"], ["tone disagreement or low confidence"])
        self.assertEqual(merged["metadataAdjudication"]["originalAgency"], "Reuters")

    def test_event_adjudication_clears_only_event_hold(self):
        merged = {
            "eventType": None,
            "eventConfidence": 0.6,
            "eventEvidence": [],
            "autoApplicable": {"eventType": False},
            "reviewReasons": [
                "event-type disagreement or low confidence",
                "tone disagreement or low confidence",
            ],
        }
        decision = {
            "eventType": "Unfacilitated",
            "confidence": 0.93,
            "eventTrigger": "unexpected crash",
            "evidence": "The aircraft crashed during training.",
            "rationale": "The unexpected crash directly triggered publication.",
        }
        RECONCILE.apply_event_adjudication("42", merged, decision)
        self.assertEqual(merged["eventType"], "Unfacilitated")
        self.assertTrue(merged["autoApplicable"]["eventType"])
        self.assertEqual(merged["reviewReasons"], ["tone disagreement or low confidence"])

    def test_domain_adjudication_resolves_only_matching_reviewer_request(self):
        metadata_request = {"review_required": True, "review_reason": "Outlet country uncertain"}
        tone_request = {"review_required": True, "review_reason": "Tone remains ambiguous"}
        self.assertTrue(
            RECONCILE.reviewer_request_resolved(metadata_request, False, True, False)
        )
        self.assertFalse(
            RECONCILE.reviewer_request_resolved(tone_request, False, True, False)
        )
        self.assertTrue(
            RECONCILE.reviewer_request_resolved(tone_request, False, False, False, True)
        )


if __name__ == "__main__":
    unittest.main()
