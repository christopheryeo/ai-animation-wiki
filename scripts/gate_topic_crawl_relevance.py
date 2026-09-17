#!/usr/bin/env python3
"""Write source-body relevance dispositions for a frozen topic-crawl SET A."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_env import load_local_env

API_URL = "https://api.openai.com/v1/responses"
SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["assessments"],
    "properties": {"assessments": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["uri", "disposition", "confidence", "reason"],
        "properties": {
            "uri": {"type": "string"},
            "disposition": {"type": "string", "enum": ["relevant", "off-topic", "held"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
        },
    }}},
}


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
            for line in text.split("---", 2)[1].splitlines() if ":" in line}


def topic_context(topic_id: str, path: Path) -> dict[str, object]:
    """Build the complete source-of-truth relevance boundary for one Topic Entity."""
    fm = frontmatter(path)
    note = path.read_text(encoding="utf-8")
    raw_aliases = yaml.safe_load(fm.get("aliases") or "[]")
    aliases = raw_aliases if isinstance(raw_aliases, list) else [raw_aliases]
    return {
        "topicId": topic_id,
        "displayName": fm["displayName"],
        "aliases": [str(value).strip() for value in aliases if str(value).strip()],
        "definition": note.split("## Definition", 1)[1].split("##", 1)[0].strip(),
        "crawlPrompt": note.split("## Crawl Prompt", 1)[1]
        .split("##", 1)[0].replace("```text", "").replace("```", "").strip(),
    }


def response_text(payload: dict) -> str:
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                return content["text"]
    raise RuntimeError("model returned no output text")


def evaluate(key: str, model: str, topic: dict[str, str], records: list[dict]) -> list[dict]:
    prompt = json.dumps({
        "task": "Classify each supplied provider article for relevance to exactly one topic.",
        "rules": [
            "Use only the supplied title, metadata, and source body.",
            "relevant means the article substantively fits the topic definition, aliases, and crawl prompt.",
            "Apply every material inclusion, exclusion, geographic, and commercial boundary stated in the topic; a keyword match alone is never enough.",
            "off-topic means it clearly does not fit; held means evidence is insufficient or borderline.",
            "Return exactly one assessment for every supplied URI; do not use outside knowledge.",
        ],
        "topic": topic,
        "articles": records,
    }, ensure_ascii=False)
    request = urllib.request.Request(API_URL, data=json.dumps({
        "model": model, "input": prompt, "store": False,
        "reasoning": {"effort": "low"},
        "text": {"format": {"type": "json_schema", "name": "topic_relevance", "strict": True, "schema": SCHEMA}},
    }, ensure_ascii=False).encode(), method="POST", headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            return json.loads(response_text(json.loads(response.read().decode())))["assessments"]
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"relevance request failed: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=20,
                        help="candidates per model call (default 20; bodies are truncated to 5000 chars each)")
    parser.add_argument("--model", default="gpt-5.6")
    parser.add_argument("--only", action="append", help="process one topic ID; repeatable")
    parser.add_argument("--offset", type=int, default=0, help="candidate offset for a bounded checkpoint")
    parser.add_argument("--limit", type=int, help="maximum candidates for a bounded checkpoint")
    args = parser.parse_args()
    load_local_env()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("OPENAI_API_KEY is unavailable")
    output = {"operation": "setA_relevance_gate", "model": args.model, "topics": []}
    topic_ids = args.only or sorted({path.name.split(".setA", 1)[0]
                                    for path in args.run_dir.glob("*.setA*.raw.json")
                                    if ".setA" in path.name})
    if not topic_ids:
        raise SystemExit("no SET A raw pages found for the requested topics")
    for topic_id in topic_ids:
        names = [path.name for path in sorted(args.run_dir.glob(f"{topic_id}.setA*.raw.json"))]
        if not names:
            raise SystemExit(f"{topic_id}: no SET A raw pages found")
        topic = topic_context(topic_id, ROOT / "entities" / "topic" / f"{topic_id}.md")
        seen, candidates = set(), []
        for name in names:
            for article in json.loads((args.run_dir / name).read_text(encoding="utf-8"))["articles"]["results"]:
                uri = str(article.get("uri") or "")
                if not uri or uri in seen:
                    continue
                seen.add(uri)
                candidates.append({"uri": uri, "title": article.get("title", ""), "date": article.get("dateTimePub") or article.get("date", ""), "url": article.get("url", ""), "body": (article.get("body") or "")[:5000]})
        candidates = candidates[args.offset: args.offset + args.limit if args.limit is not None else None]
        assessments = []
        for start in range(0, len(candidates), args.batch_size):
            print(f"{topic_id}: reviewing {start + 1}-{min(start + args.batch_size, len(candidates))} of {len(candidates)}", flush=True)
            assessments.extend(evaluate(key, args.model, topic, candidates[start:start + args.batch_size]))
        if {x["uri"] for x in assessments} != {x["uri"] for x in candidates}:
            raise RuntimeError(f"{topic_id}: model did not return one assessment per candidate")
        output["topics"].append({"topicId": topic_id, "candidateCount": len(candidates), "assessments": assessments})
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "topics": len(output["topics"]), "candidates": sum(x["candidateCount"] for x in output["topics"]) }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
