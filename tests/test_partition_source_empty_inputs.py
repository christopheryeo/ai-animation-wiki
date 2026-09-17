import hashlib
import importlib.util
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


PARTITION = load_script("partition_source_empty_inputs")


class PartitionSourceEmptyInputsTests(unittest.TestCase):
    def note(self, article_id, empty=False):
        if empty:
            return (
                "---\n"
                f"articleId: '{article_id}'\n"
                "articleTitle: ''\n"
                "publishedDate: ''\n"
                "url: ''\n"
                "rawNewsApiResponse: '{}'\n"
                "---\n\n"
            )
        return (
            "---\n"
            f"articleId: '{article_id}'\n"
            "articleTitle: 'Usable article'\n"
            "publishedDate: '2026-08-15T01:00:00Z'\n"
            "url: 'https://example.test/article'\n"
            "rawNewsApiResponse: '{\"uri\":\"1\"}'\n"
            "---\n\nSaved source body.\n"
        )

    def run_partition(self, notes):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            base = pathlib.Path(folder)
            articles = []
            for filename, text in notes:
                path = base / filename
                path.write_text(text, encoding="utf-8")
                articles.append({
                    "path": str(path.relative_to(ROOT)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                })
            master = base / "master.json"
            master.write_text(json.dumps({
                "articleCount": len(articles),
                "articles": articles,
            }), encoding="utf-8")
            argv = [
                "partition_source_empty_inputs.py",
                "--master-manifest", str(master),
                "--hold-dir", str(base / "holds"),
                "--hold-manifest", str(base / "holds.json"),
                "--eligible-manifest", str(base / "eligible.txt"),
                "--eligible-evidence", str(base / "eligible.json"),
            ]
            with mock.patch.object(sys, "argv", argv):
                result = PARTITION.main()
            return result, json.loads((base / "holds.json").read_text()), json.loads(
                (base / "eligible.json").read_text()
            )

    def test_empty_stub_may_share_id_with_one_eligible_article(self):
        result, holds, eligible = self.run_partition([
            ("same-untitled.md", self.note("same", empty=True)),
            ("same-usable.md", self.note("same")),
        ])
        self.assertEqual(result, 0)
        self.assertEqual(holds["holdCount"], 1)
        self.assertEqual(eligible["articleCount"], 1)

    def test_two_eligible_articles_with_same_id_are_rejected(self):
        with self.assertRaisesRegex(PARTITION.PartitionError, "duplicate eligible article ID"):
            self.run_partition([
                ("same-one.md", self.note("same")),
                ("same-two.md", self.note("same")),
            ])


if __name__ == "__main__":
    unittest.main()
