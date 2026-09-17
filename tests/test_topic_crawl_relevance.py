import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gate_topic_crawl_relevance", ROOT / "scripts" / "gate_topic_crawl_relevance.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def test_topic_context_includes_aliases_and_scope_boundaries(tmp_path):
    note = tmp_path / "example-studio.md"
    note.write_text(
        "---\n"
        "displayName: Example Studio\n"
        "aliases: [Example Animation, ES]\n"
        "---\n\n"
        "## Definition\n"
        "US and Southeast Asian animation-studio business activity; exclude live-action-only news.\n\n"
        "## Crawl Prompt\n"
        "```text\n(Example Studio OR Example Animation) AND animation\n```\n",
        encoding="utf-8",
    )

    context = MODULE.topic_context("example-studio", note)

    assert context["aliases"] == ["Example Animation", "ES"]
    assert "Southeast Asian" in context["definition"]
    assert "Example Animation" in context["crawlPrompt"]
