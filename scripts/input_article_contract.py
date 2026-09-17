"""Shared completeness contract for enriched article inputs.

Both enrichment and cascade use this module so their definition of a
``cascade-ready`` article cannot drift. The validator is deliberately pure:
it reports findings and never edits the article.
"""
from __future__ import annotations

from typing import Any


INSTITUTIONAL_CATEGORIES = (
    "Animation Studio", "Character and IP", "Merchandising and Licensing",
    "Distribution and Platforms", "Technology and Production", "Industry Policy",
    "Studio Business", "Non-institutional",
)
TONE_VALUES = ("Factual", "Opinionated")
SENTIMENT_VALUES = ("Positive", "Neutral", "Negative")
EVENT_VALUES = ("Facilitated", "Unfacilitated")
SOURCE_TYPE_VALUES = ("feed", "crawl")

COMPLETE_INPUT_FIELDS = (
    "articleId", "articleTitle", "publishedDate", "category", "topic", "tone",
    "toneSentiment", "eventType", "tags", "outlets", "countries", "coverageCount",
    "mediaCount", "sourceType", "url",
)


def complete_input_findings(data: dict[str, Any], body: str) -> list[dict[str, str]]:
    """Return every reason an enriched input is not safe to cascade."""

    findings: list[dict[str, str]] = []
    for field in COMPLETE_INPUT_FIELDS:
        if field not in data:
            findings.append({"field": field, "issue": "missing"})

    for field, allowed in {
        "category": INSTITUTIONAL_CATEGORIES,
        "tone": TONE_VALUES,
        "toneSentiment": SENTIMENT_VALUES,
        "eventType": EVENT_VALUES,
        "sourceType": SOURCE_TYPE_VALUES,
    }.items():
        if data.get(field) not in allowed:
            findings.append({"field": field, "issue": "invalid value"})

    for field in ("articleId", "articleTitle", "publishedDate", "topic", "url"):
        if not data.get(field):
            findings.append({"field": field, "issue": "empty"})

    for field in ("tags", "outlets", "countries"):
        if not isinstance(data.get(field), list):
            findings.append({"field": field, "issue": "not a list"})
    if not data.get("outlets"):
        findings.append({"field": "outlets", "issue": "empty"})

    for field in ("coverageCount", "mediaCount"):
        value = data.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            findings.append({"field": field, "issue": "not a non-negative integer"})

    if not body.strip():
        findings.append({"field": "body", "issue": "empty"})
    return findings
