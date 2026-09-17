import importlib.util
import pathlib
import sys
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("topic_crawl_map_url", SCRIPTS / "topic_crawl_map_url.py")
MAPPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MAPPER)


class TopicCrawlMapUrlTests(unittest.TestCase):
    def test_null_mapper_value_requires_fallback(self):
        self.assertIsNone(MAPPER.mapped_uri({"https://example.test/a": None}, "https://example.test/a"))

    def test_string_mapper_value_is_usable_uri(self):
        self.assertEqual("urn:news:article", MAPPER.mapped_uri(
            {"https://example.test/a": "urn:news:article"}, "https://example.test/a"))


if __name__ == "__main__":
    unittest.main()
