import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("query_under_test", ROOT / "scripts" / "query.py")
QUERY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = QUERY
SPEC.loader.exec_module(QUERY)


class QueryStarterTests(unittest.TestCase):
    def test_empty_catalogs_resolve_no_entities(self):
        self.assertEqual(QUERY._catalog_entity_matches("Who owns this character?"), [])

    def test_empty_wiki_has_no_fast_context(self):
        self.assertIsNone(QUERY.build_fast_context("Which studios are monitored?"))

    def test_slug_is_stable_and_filesystem_safe(self):
        self.assertEqual(QUERY._slugify("AI Animation: Studio & Character News"), "ai-animation-studio-character-news")

    def test_flags_honor_explicit_off(self):
        self.assertEqual(QUERY.resolve_flags(False, False), (False, False))

    def test_tools_can_be_built_without_network_access(self):
        names = {tool["name"] for tool in QUERY.build_tools(cache_read=False)}
        self.assertIn("resolve_entity", names)
        self.assertNotIn("search_cache", names)


if __name__ == "__main__":
    unittest.main()
