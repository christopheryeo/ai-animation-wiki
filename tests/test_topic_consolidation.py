import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "topic_consolidation", ROOT / "scripts" / "topic_consolidation.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_taxonomy_is_inside_approved_range_and_unique():
    payload, topics = MODULE.load_taxonomy()
    ids = [row["topicId"] for row in topics]
    assert 60 <= len(topics) <= payload["maximumActiveTopics"] <= MODULE.ABSOLUTE_MAXIMUM_ACTIVE_TOPICS
    assert len(ids) == len(set(ids))
    assert payload["assignmentLimit"] == 3


def test_weak_nonzero_score_still_selects_primary():
    _, topics = MODULE.load_taxonomy()
    fragments = {
        "sourceId": "x",
        "title": "Animation licensing",
        "projectionTopic": "",
        "issueTags": [],
        "topicLinks": [],
        "summary": "",
        "keyPoints": "",
        "category": "",
    }
    result = MODULE.classify(fragments, topics)
    assert result["primary"] == "animation-licensing"
    assert not result["fallback"]


def test_unmatched_article_stays_unassigned_for_relevance_review():
    _, topics = MODULE.load_taxonomy()
    fragments = {
        "sourceId": "x",
        "title": "Completely unrelated placeholder",
        "projectionTopic": "",
        "issueTags": [],
        "topicLinks": [],
        "summary": "",
        "keyPoints": "",
        "category": "",
    }
    result = MODULE.classify(fragments, topics)
    assert result["primary"] == ""
    assert result["fallback"]
