#!/usr/bin/env python3
"""Append terminal SET A relevance dispositions to a goal-crawl ledger.

Only ``off-topic`` and ``held`` candidates are terminal at this stage; relevant
candidates continue through the remaining source and cascade gates.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    run_dir = args.run_dir
    raw_by_uri: dict[str, dict] = {}
    for path in run_dir.glob("*.setA*.raw.json"):
        for article in load_json(path).get("articles", {}).get("results", []):
            uri = str(article.get("uri") or "")
            if uri:
                raw_by_uri.setdefault(uri, article)
    ledger = run_dir / "candidates.ndjson"
    existing = {json.loads(line).get("candidateId") for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()}
    decisions: dict[tuple[str, str], tuple[dict, str]] = {}
    for path in sorted(run_dir.glob("setA-relevance-*.json")):
        for topic in load_json(path).get("topics", []):
            topic_id = str(topic.get("topicId") or "")
            for item in topic.get("assessments", []):
                uri = str(item.get("uri") or "")
                if topic_id and uri:
                    decisions.setdefault((topic_id, uri), (item, path.name))
    rows = []
    for (topic_id, uri), (item, artifact_name) in sorted(decisions.items()):
        disposition = str(item.get("disposition") or "")
        if disposition not in {"off-topic", "held"}:
            continue
        candidate_id = f"setA:{topic_id}:{uri}"
        if candidate_id in existing:
            continue
        article = raw_by_uri.get(uri, {})
        row = {
            "candidateId": candidate_id, "topicId": topic_id, "set": "A",
            "providerUri": uri, "canonicalUrl": str(article.get("url") or ""),
            "disposition": disposition, "reason": str(item.get("reason") or "source-body relevance decision"),
            "relevanceConfidence": item.get("confidence"), "relevanceArtifact": artifact_name,
        }
        if disposition == "held":
            row["holdStage"] = "source-body relevance"
        rows.append(row)
    if rows:
        with ledger.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"appended": len(rows), "ledger": str(ledger)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
