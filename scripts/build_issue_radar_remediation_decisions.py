#!/usr/bin/env python3
"""Build a reviewer-owned issue-tag decision ledger from a frozen local evidence pack.

This generic starter utility performs no network calls. It suggests a bounded
AI Animation tag only when saved evidence contains an explicit domain signal;
all other cases stop for human review.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


RULES = (
    (("character", "franchise", "intellectual property"), "Character and IP"),
    (("license", "licensing", "royalty"), "Licensing"),
    (("merchandise", "merchandising", "consumer product"), "Merchandising"),
    (("studio", "production company"), "Animation Studios"),
    (("streaming", "distribution", "platform"), "Distribution"),
    (("render", "production pipeline", "animation technology"), "Animation Technology"),
    (("funding", "investment", "acquisition"), "Studio Investment"),
)


def choose_tag(evidence: dict) -> tuple[str, str]:
    text = " ".join(str(evidence.get(key) or "") for key in (
        "title", "summary", "keyPoints", "sourceText", "relatedEntities", "projectionTopic"
    )).casefold()
    for needles, tag in RULES:
        if any(needle in text for needle in needles):
            return tag, f"Saved evidence explicitly supports the {tag} classification."
    raise ValueError("no evidence-backed AI Animation issue tag; human review required")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pack = json.loads((args.bundle_dir / "local-evidence-pack.json").read_text(encoding="utf-8"))
    decisions = []
    for item in pack.get("items", []):
        if item.get("field") != "issueTags":
            continue
        tag, rationale = choose_tag(item.get("evidence") or {})
        decisions.append({
            "sourceId": item.get("sourceId"),
            "field": "issueTags",
            "index": item.get("index"),
            "newValue": [tag],
            "rationale": rationale,
            "status": "proposed",
        })
    args.output.write_text(json.dumps({"decisions": decisions}, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
