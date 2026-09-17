#!/usr/bin/env python3
"""Generate source-search prompts from the canonical AI Animation topic registry."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import yaml


ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "topics" / "canonical-topics.yaml"
REGION_GUARD = '("United States" OR "Southeast Asia")'
DOMAIN_GUARD = '(animation OR character OR merchandising OR licensing OR studio)'


def load_topics() -> list[dict]:
    payload = yaml.safe_load(TAXONOMY.read_text(encoding="utf-8")) or {}
    topics = payload.get("topics")
    if not isinstance(topics, list):
        raise ValueError("canonical taxonomy has no topics list")
    return topics


def build_prompt(topic: dict) -> str:
    keywords = [str(value).strip() for value in topic.get("keywords") or [] if str(value).strip()]
    if not keywords:
        raise ValueError(f"topic {topic.get('topicId')!r} has no keywords")
    terms = " OR ".join(f'"{value}"' for value in keywords)
    return f"({terms}) AND {DOMAIN_GUARD} AND {REGION_GUARD}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    rows = [{"topicId": row["topicId"], "prompt": build_prompt(row)} for row in load_topics()]
    print(json.dumps(rows, indent=2) if args.json else "\n".join(f"{r['topicId']}: {r['prompt']}" for r in rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
