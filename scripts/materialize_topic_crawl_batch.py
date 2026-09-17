#!/usr/bin/env python3
"""Materialize a reconciled, relevance-approved SET A into frozen crawl inputs.

This is deliberately narrow: it reads only retained provider responses and
relevance checkpoints, deduplicates by provider URI/URL, and writes the raw
input contract.  It does not invent source text or perform enrichment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[1]


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "untitled"
    return result[:108].rstrip("-")


def q(value: object) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def raw_records(run_dir: Path) -> dict[str, dict]:
    records: dict[str, dict] = {}
    for source in run_dir.glob("*.setA*.raw.json"):
        try:
            items = json.loads(source.read_text(encoding="utf-8"))["articles"]["results"]
        except (OSError, ValueError, KeyError):
            continue
        for item in items:
            uri = str(item.get("uri") or "")
            if uri:
                records.setdefault(uri, item)
    return records


def assessments(run_dir: Path) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted(run_dir.glob("setA-relevance-*.json")):
        # Only bounded checkpoints are authoritative.  Aggregate/full-topic
        # files duplicate them and may have been written before all chunks.
        if not re.search(r"-\d+\.json$", path.name):
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for topic in data.get("topics", []):
            topic_id = topic.get("topicId", "")
            for item in topic.get("assessments", []):
                key = (str(topic_id), str(item.get("uri") or ""))
                if item.get("disposition") == "relevant" and key not in seen:
                    rows.append((topic_id, item))
                    seen.add(key)
    return rows


def topic_display(topic_id: str) -> str:
    note = ROOT / "entities" / "topic" / f"{topic_id}.md"
    metadata = yaml.safe_load(note.read_text(encoding="utf-8").split("---", 2)[1]) or {}
    return str(metadata["displayName"])


def existing_urls() -> set[str]:
    """Collect preserved source URLs from current raw and compiled notes."""
    result: set[str] = set()
    for root in (ROOT / "Inputs" / "articles", ROOT / "entities" / "article"):
        for path in root.rglob("*.md"):
            try:
                metadata = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1]) or {}
            except (OSError, IndexError, yaml.YAMLError):
                continue
            url = str(metadata.get("url") or "").strip()
            if url:
                result.add(url)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--manifest-dir", type=Path, required=True)
    args = parser.parse_args()
    records = raw_records(args.run_dir)
    known_urls = existing_urls()
    selected: dict[str, dict] = {}
    duplicate_topics: dict[str, list[str]] = {}
    held: list[dict] = []
    for topic_id, assessment in assessments(args.run_dir):
        uri = str(assessment.get("uri") or "")
        article = records.get(uri)
        if not article or not article.get("url") or not article.get("body") or not article.get("date"):
            held.append({"uri": uri, "topicId": topic_id, "reason": "missing retained provider identity, date, URL, or body"})
            continue
        if uri in selected:
            duplicate_topics.setdefault(uri, [selected[uri]["topicId"]]).append(topic_id)
            continue
        selected[uri] = {"topicId": topic_id, "assessment": assessment, "article": article}
    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    manifests: dict[str, list[str]] = {}
    output_rows: list[dict] = []
    for uri, row in sorted(selected.items(), key=lambda pair: (pair[1]["article"]["date"], pair[0])):
        article, topic_id = row["article"], row["topicId"]
        published = str(article["date"])[:10]
        try:
            month = date.fromisoformat(published).strftime("%Y-%m")
        except ValueError:
            held.append({"uri": uri, "topicId": topic_id, "reason": "invalid publication date"})
            continue
        canonical_url = str(article["url"])
        article_id = "crawl-" + hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        filename = f"{article_id}-{slug(str(article.get('title') or 'untitled'))}.md"
        target = ROOT / "Inputs" / "articles" / month / filename
        if target.exists() or canonical_url in known_urls:
            output_rows.append({"uri": uri, "topicId": topic_id, "filename": str(target.relative_to(ROOT)), "disposition": "duplicate-existing"})
            continue
        source = article.get("source") or {}
        outlet = source.get("title") or source.get("uri") or "Unknown outlet"
        # Intake `tags` holds only active issue tags (assigned later by enrichment).
        # The control tag `#source` is added by the compiler (ingest_cascade), not here —
        # seeding it as an issue tag makes resolve_issue_tags reject the note at cascade.
        text = "\n".join([
            "---", f"articleId: {q(article_id)}", f"articleTitle: {q(article.get('title'))}",
            f"publishedDate: {q(published)}", "category: Non-institutional",
            f"topic: {q(topic_display(topic_id))}", "tone: Factual", "toneSentiment: Neutral",
            "eventType: Unfacilitated", "tags: []", f"outlets: [{q(outlet)}]",
            "countries: []", "coverageCount: 1", "mediaCount: 0", "sourceType: crawl",
            f"url: {q(canonical_url)}", "---", "", str(article["body"]).rstrip(), "",
        ])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        manifests.setdefault(month, []).append(filename)
        output_rows.append({"uri": uri, "topicId": topic_id, "filename": str(target.relative_to(ROOT)), "disposition": "materialized"})
    manifest_paths = {}
    for month, names in manifests.items():
        path = args.manifest_dir / f"{month}.txt"
        path.write_text("\n".join(names) + "\n", encoding="utf-8")
        manifest_paths[month] = str(path)
    payload = {"setARelevantAssessments": len(assessments(args.run_dir)), "uniqueEligible": len(selected), "crossTopicDuplicates": duplicate_topics, "held": held, "manifests": manifest_paths, "rows": output_rows}
    args.artifact.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"relevantAssessments": payload["setARelevantAssessments"], "uniqueEligible": payload["uniqueEligible"], "materialized": sum(1 for row in output_rows if row["disposition"] == "materialized"), "held": len(held), "crossTopicDuplicates": len(duplicate_topics)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
