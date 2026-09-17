import importlib.util
import inspect
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_script(name):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROJECTION = load_script("wiki_uat_projection")
IMPORTER = load_script("uat_to_inputs")
CASCADE = load_script("ingest_cascade")
PROJECTOR = load_script("project_wiki_to_uat")


class WikiUatReconciliationTests(unittest.TestCase):
    def projection(self):
        return {
            "schemaVersion": "wiki-uat-projection.v1",
            "identity": {
                "wikiSourceId": "uat-legacy-42",
                "uatArticleId": 42,
                "origin": "uat-legacy",
            },
            "article": {
                "article_id": 42,
                "document_id": 7,
                "article_title": "A title",
                "content_title": "A content title",
                "content_description": "Source prose.",
                "published_date": "2026-03-01 01:02:03",
                "category": "Animation Policy",
                "topic": "Readiness",
                "tone": "Factual",
                "tone_sentiment": "Neutral",
                "event_type": "Facilitated",
            },
            "coverage": [{
                "coverage_id": None,
                "coverage_type": "online",
                "display_name": "Example",
                "country": "Southeast Asia",
                "media_outlet_category": None,
                "url": None,
            }],
            "media": [],
            "tags": ["Readiness", "Readiness"],
            "userGroups": [1],
        }

    def test_cascade_separates_projection_from_source_prose(self):
        projection = self.projection()
        body = (
            "Human source prose.\n\n"
            + PROJECTION.render_projection(projection)
        )
        source, rendered = CASCADE.split_database_projection(body)
        self.assertEqual(source, "Human source prose.")
        self.assertEqual(json.loads(rendered), projection)

    def test_cascade_builds_complete_unallocated_projection(self):
        rendered = CASCADE.build_provisional_database_projection(
            {
                "category": "National Security",
                "tone": "Factual",
                "toneSentiment": "Neutral",
                "eventType": "Facilitated",
            },
            "new-source",
            "New title",
            "Saved source prose.",
            ["Example"],
            ["Southeast Asia"],
            ["Readiness"],
            "https://example.com/story",
            "crawl",
            "2026-07-29T12:00:00Z",
        )
        projection = json.loads(rendered)
        self.assertIsNone(projection["identity"]["uatArticleId"])
        self.assertIsNone(projection["article"]["article_id"])
        self.assertEqual(projection["article"]["content_description"], "Saved source prose.")
        self.assertEqual(projection["coverage"][0]["display_name"], "Example")
        allocated = PROJECTOR.finalize_provisional_projection(
            "new-source", projection, 123,
        )
        self.assertEqual(allocated["identity"]["uatArticleId"], 123)
        self.assertEqual(allocated["article"]["article_id"], 123)
        self.assertEqual(allocated["article"]["published_date"], "2026-07-29 12:00:00.000000")
        self.assertTrue(PROJECTOR._datetime_semantically_equal(
            "2026-07-29 12:00:00",
            "2026-07-29 12:00:00.000000",
        ))

    def test_cascade_manifest_rejects_duplicate_filenames(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = pathlib.Path(folder) / "manifest.txt"
            manifest.write_text("Inputs/articles/one.md\nother/one.md\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                CASCADE.load_manifest_names(manifest)

    def test_cascade_manifest_loads_unique_markdown_filenames(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = pathlib.Path(folder) / "manifest.txt"
            manifest.write_text("Inputs/articles/one.md\nInputs/articles/two.md\n", encoding="utf-8")
            self.assertEqual(CASCADE.load_manifest_names(manifest), {"one.md", "two.md"})

    def test_cascade_assigns_only_registered_canonical_topics(self):
        selected = CASCADE.canonical_topic_selection(
            "A studio announced a new animated character",
            "The announcement covers licensing and merchandising.",
            {"topic": "Character Licensing", "category": "Studio Business"},
            ["Character Licensing"],
        )
        canonical_ids = {
            row["topicId"] for row in CASCADE.load_taxonomy()[1]
        }
        self.assertEqual(selected, [("animation-licensing", "Animation Licensing")])
        self.assertTrue({topic_id for topic_id, _ in selected} <= canonical_ids)

    def test_import_note_preserves_null_url_and_duplicate_tags(self):
        note = IMPORTER.render_input_note(self.projection())
        metadata, body = CASCADE.parse_frontmatter(note)
        self.assertEqual(metadata["articleId"], "uat-legacy-42")
        self.assertEqual(metadata["url"], "")
        self.assertEqual(metadata["tags"], ["Readiness", "Readiness"])
        source, projection = PROJECTION.extract_projection(body)
        self.assertEqual(source, "Source prose.")
        self.assertEqual(projection["coverage"][0]["url"], None)

    def test_read_only_mysql_rejects_mutation_and_production(self):
        with self.assertRaises(PROJECTION.ProjectionError):
            PROJECTION.run_mysql("INSERT INTO UAT_articles VALUES (1)")
        with self.assertRaises(PROJECTION.ProjectionError):
            PROJECTION.run_mysql("SELECT 1 FROM AI_Animation.articles")
        with self.assertRaises(PROJECTION.ProjectionError):
            PROJECTION.run_production_readonly("DELETE FROM articles")

    def test_manifest_detects_tampering_and_extra_files(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = pathlib.Path(folder)
            payload = bundle / "payload.txt"
            payload.write_text("original", encoding="utf-8")
            PROJECTION.write_hashed_manifest(bundle, {"version": 1}, [payload])
            PROJECTION.verify_hashed_manifest(bundle)
            payload.write_text("changed", encoding="utf-8")
            with self.assertRaises(PROJECTION.ProjectionError):
                PROJECTION.verify_hashed_manifest(bundle)

    def test_apply_refuses_overwrite_before_copying_any_file(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle = pathlib.Path(folder) / "bundle"
            inputs = bundle / "inputs" / "2026-03"
            inputs.mkdir(parents=True)
            for index in range(IMPORTER.EXPECTED_MONTHS["2026-03"]):
                (inputs / f"uat-legacy-{index}.md").write_text("x", encoding="utf-8")
            collision_root = pathlib.Path(folder) / "destination"
            destination = collision_root / "2026-03"
            destination.mkdir(parents=True)
            (destination / "uat-legacy-0.md").write_text("existing", encoding="utf-8")
            args = type("Args", (), {
                "bundle_dir": bundle,
                "month": "2026-03",
            })()
            with (
                mock.patch.object(IMPORTER, "verify_bundle_dir"),
                mock.patch.object(IMPORTER, "INPUT_ROOT", collision_root),
                self.assertRaises(PROJECTION.ProjectionError),
            ):
                IMPORTER.apply(args)
            self.assertEqual(len(list(destination.glob("*.md"))), 1)

    def test_empty_starter_has_no_frozen_review_population(self):
        self.assertFalse(PROJECTOR.RECONCILIATION.exists())
        self.assertFalse(PROJECTOR.DIRECT_REVIEW.exists())

    def test_load_sql_is_uat_only_transaction_with_rollback_handler(self):
        projection = self.projection()
        for column in PROJECTION.ARTICLE_COLUMNS:
            projection["article"].setdefault(column, None)
        sql = PROJECTOR.render_load_sql([projection], "abc123")
        self.assertIn("USE `AI_Animation_UAT`;", sql)
        self.assertIn("START TRANSACTION;", sql)
        self.assertIn("ROLLBACK;", sql)
        self.assertIn("RESIGNAL;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertNotIn("USE `AI_Animation`;", sql)

    def test_sync_sql_supports_exact_uat_updates_and_child_refresh(self):
        projection = self.projection()
        for column in PROJECTION.ARTICLE_COLUMNS:
            projection["article"].setdefault(column, None)
        sql = PROJECTOR.render_sync_sql([], [projection], "update123", 1, 1)
        self.assertIn("USE `AI_Animation_UAT`;", sql)
        self.assertIn("UPDATE `UAT_articles` SET", sql)
        self.assertIn("DELETE FROM `UAT_article_tags` WHERE `article_id` IN (42);", sql)
        self.assertIn("INSERT INTO `UAT_article_tags`", sql)
        self.assertIn("ROLLBACK;", sql)
        self.assertIn("COMMIT;", sql)
        self.assertNotIn("USE `AI_Animation`;", sql)

    def test_sql_literal_preserves_crlf_without_physical_line_drift(self):
        rendered = PROJECTOR._sql_literal("one\r\ntwo")
        self.assertEqual(rendered, "'one\\r\\ntwo'")
        self.assertNotIn("\r", rendered)

    def test_approval_must_be_attributed_and_bound_to_bundle(self):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "approval.json"
            path.write_text(json.dumps({
                "approved": True,
                "approvedBy": "Christopher",
                "approvedAt": "2026-07-29T15:00:00+08:00",
                "bundleId": "wrong",
            }), encoding="utf-8")
            with self.assertRaises(PROJECTION.ProjectionError):
                PROJECTOR._verify_approval(path, {"bundleId": "expected"})

    def test_topic_precondition_read_is_bounded_to_approved_ids(self):
        with mock.patch.object(
            PROJECTOR,
            "run_mysql",
            return_value="7\tH416972204361706162696C6974696573\n9\tN\n",
        ) as runner:
            rows = PROJECTOR._topic_rows_from_uat({9, 7})
        self.assertEqual(rows, {7: "Air Capabilities", 9: None})
        self.assertIn("WHERE `article_id` IN (7,9)", runner.call_args.args[0])

    def test_cascade_is_separate_from_projection_and_database_commands(self):
        self.assertFalse(hasattr(CASCADE, "prepare_verified_uat_delta"))
        source = inspect.getsource(CASCADE.run_batch)
        self.assertNotIn("project_wiki_to_uat", source)
        self.assertNotIn("prepare-current", source)
        self.assertNotIn("apply-projections", source)
        self.assertNotIn("diff", source)


if __name__ == "__main__":
    unittest.main()
