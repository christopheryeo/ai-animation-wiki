import importlib.util
import pathlib
import sys
import unittest


SCRIPT_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location("article_quality", SCRIPT_DIR / "article_quality.py")
QUALITY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALITY)


class ArticleQualityTests(unittest.TestCase):
    def test_hyphenated_crawler_id_matches_filename_prefix(self):
        path = pathlib.Path(
            "entities/article/2026-08/2026-08-1251622954-example.md"
        )
        self.assertEqual(
            QUALITY.expected_source_id(path, "2026-08-1251622954"),
            "2026-08-1251622954",
        )


if __name__ == "__main__":
    unittest.main()
