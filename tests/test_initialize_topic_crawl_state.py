import importlib.util
import pathlib
import tempfile
import unittest


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "initialize_topic_crawl_state",
    SCRIPT_DIR / "initialize_topic_crawl_state.py",
)
STATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STATE)


class TopicCrawlStateTests(unittest.TestCase):
    def test_monthly_log_is_not_treated_as_topic_entity(self):
        with tempfile.TemporaryDirectory() as temporary:
            topic_root = pathlib.Path(temporary)
            (topic_root / "air-capabilities.md").write_text(
                "---\ntopicId: air-capabilities\narticleCount: 0\nlastCrawledAt: null\n"
                "crawlStatus: Not started\ncrawlStatusAt: null\n---\n",
                encoding="utf-8",
            )
            (topic_root / "log-2026-08.md").write_text(
                "# Topic Audit Log — 2026-08\n",
                encoding="utf-8",
            )
            original = STATE.TOPIC_ROOT
            try:
                STATE.TOPIC_ROOT = topic_root
                self.assertEqual(
                    [path.name for path in STATE.topic_paths()],
                    ["air-capabilities.md"],
                )
                self.assertEqual(
                    STATE.validate(),
                    {"topics": 1, "validFields": 1, "validStatuses": 1, "failures": 0},
                )
            finally:
                STATE.TOPIC_ROOT = original

    def test_initialize_preserves_queued_status_and_migrates_completed_topic(self):
        with tempfile.TemporaryDirectory() as temporary:
            topic_root = pathlib.Path(temporary)
            (topic_root / "queued.md").write_text(
                "---\ntopicId: queued\ndisplayName: Queued\narticleCount: 0\n"
                "lastCrawledAt: null\ncrawlStatus: ready-to-crawl\n"
                "crawlStatusAt: 2026-08-19T09:42:14Z\n---\n",
                encoding="utf-8",
            )
            (topic_root / "completed.md").write_text(
                "---\ntopicId: completed\ndisplayName: Completed\narticleCount: 0\n"
                "lastCrawledAt: 2026-08-18T08:00:00Z\n---\n",
                encoding="utf-8",
            )
            (topic_root / "log.md").write_text("ledger\n", encoding="utf-8")
            original = STATE.TOPIC_ROOT
            try:
                STATE.TOPIC_ROOT = topic_root
                result = STATE.initialize(dry_run=False)
                self.assertEqual(result["changed"], 2)
                self.assertIn("crawlStatus: Queued", (topic_root / "queued.md").read_text())
                completed = (topic_root / "completed.md").read_text()
                self.assertIn("crawlStatus: Completed", completed)
                self.assertIn("crawlStatusAt: 2026-08-18T08:00:00Z", completed)
                self.assertEqual(STATE.validate()["validStatuses"], 2)
            finally:
                STATE.TOPIC_ROOT = original

    def test_conflicted_copy_is_not_an_active_topic(self):
        with tempfile.TemporaryDirectory() as temporary:
            topic_root = pathlib.Path(temporary)
            (topic_root / "topic.md").write_text("---\ntopicId: topic\n---\n", encoding="utf-8")
            (topic_root / "topic (Chris Yeo's conflicted copy 2026-08-18).md").write_text(
                "---\ntopicId: topic\n---\n", encoding="utf-8"
            )
            original = STATE.TOPIC_ROOT
            try:
                STATE.TOPIC_ROOT = topic_root
                self.assertEqual([path.name for path in STATE.topic_paths()], ["topic.md"])
            finally:
                STATE.TOPIC_ROOT = original


if __name__ == "__main__":
    unittest.main()
