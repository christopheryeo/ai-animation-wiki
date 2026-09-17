import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "topic_crawl_goal_runner.py"
SPEC = importlib.util.spec_from_file_location("topic_crawl_goal_runner", SCRIPT)
GOAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GOAL)


class TopicCrawlGoalRunnerTests(unittest.TestCase):
    def state(self):
        return {"status": "running", "topics": ["topic-a"],
                "topicCompletion": {"topic-a": {"status": "completed", "verifiedAt": "2026-09-15T09:00:00Z"}},
                "criticalEvents": []}

    def test_held_candidate_is_reconciled(self):
        row = {"_line": 1, "candidateId": "one", "topicId": "topic-a", "set": "B",
               "canonicalUrl": "https://example.test/a", "disposition": "held", "holdStage": "retrieval",
               "reason": "Provider and publisher retrieval exhausted."}
        self.assertTrue(GOAL.validate(self.state(), [row], pathlib.Path.cwd())["valid"])

    def test_unresolved_critical_event_prevents_closure(self):
        state = self.state(); state["criticalEvents"] = [{"kind": "credential-rejected", "resolved": False}]
        self.assertFalse(GOAL.validate(state, [], pathlib.Path.cwd())["valid"])

    def test_cascaded_candidate_requires_phase_evidence(self):
        row = {"_line": 1, "candidateId": "one", "topicId": "topic-a", "set": "B",
               "canonicalUrl": "https://example.test/a", "disposition": "cascaded", "reason": "In scope.",
               "intakePath": "Inputs/articles/2026-09/a.md", "articlePath": "entities/article/2026-09/a.md"}
        result = GOAL.validate(self.state(), [row], pathlib.Path.cwd())
        self.assertFalse(result["valid"])
        self.assertTrue(any("phaseEvidence" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
