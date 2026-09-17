import importlib.util
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "route_input_articles.py"
SPEC = importlib.util.spec_from_file_location("route_input_articles", SCRIPT)
ROUTER = importlib.util.module_from_spec(SPEC)
sys.modules["route_input_articles"] = ROUTER
SPEC.loader.exec_module(ROUTER)


class RouteInputArticlesTests(unittest.TestCase):
    def article(self, published="2026-07-23T07:29:40Z"):
        return (
            "---\n"
            "articleId: 'crawl-one'\n"
            "articleTitle: 'Crawler article'\n"
            f"publishedDate: '{published}'\n"
            "---\n\nBody\n"
        )

    def test_dry_run_does_not_move_loose_crawler_article(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            inputs = root / "Inputs" / "articles"
            entities = root / "entities" / "article"
            inputs.mkdir(parents=True)
            entities.mkdir(parents=True)
            source = inputs / "crawl-one.md"
            source.write_text(self.article(), encoding="utf-8")
            result = ROUTER.route_loose_articles(inputs, entities)
            self.assertEqual(result["planned"], 1)
            self.assertEqual(result["moved"], 0)
            self.assertTrue(source.exists())

    def test_write_routes_by_published_month_without_changing_content(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            inputs = root / "Inputs" / "articles"
            entities = root / "entities" / "article"
            inputs.mkdir(parents=True)
            entities.mkdir(parents=True)
            source = inputs / "crawl-one.md"
            original = self.article()
            source.write_text(original, encoding="utf-8")
            result = ROUTER.route_loose_articles(inputs, entities, write=True)
            target = inputs / "2026-07" / "crawl-one.md"
            self.assertEqual(result["moved"], 1)
            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_manifest_scope_excludes_later_arrivals(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            inputs = root / "Inputs" / "articles"
            entities = root / "entities" / "article"
            inputs.mkdir(parents=True)
            entities.mkdir(parents=True)
            selected = inputs / "selected.md"
            later = inputs / "later.md"
            selected.write_text(self.article(), encoding="utf-8")
            later.write_text(self.article(), encoding="utf-8")

            result = ROUTER.route_loose_articles(
                inputs, entities, write=True, selected_names={"selected.md"},
            )

            self.assertEqual(result["looseFiles"], 1)
            self.assertEqual(result["moved"], 1)
            self.assertTrue((inputs / "2026-07" / "selected.md").exists())
            self.assertTrue(later.exists())

    def test_frozen_manifest_records_identity_and_content_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            source = root / "crawl-one.md"
            source.write_text(self.article(), encoding="utf-8")
            original_root = ROUTER.ROOT
            ROUTER.ROOT = root
            try:
                manifest = ROUTER.build_frozen_manifest(root)
            finally:
                ROUTER.ROOT = original_root
        self.assertEqual(manifest["articleCount"], 1)
        self.assertEqual(manifest["articles"][0]["articleId"], "crawl-one")
        self.assertEqual(len(manifest["articles"][0]["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
