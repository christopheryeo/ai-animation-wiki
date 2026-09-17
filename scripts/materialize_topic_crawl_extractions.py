#!/usr/bin/env python3
"""Materialize accepted provider-extraction evidence into crawl raw inputs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def slug(value: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "untitled")[:108].rstrip("-")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accepted-manifest", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    args = parser.parse_args()
    rows = json.loads(args.accepted_manifest.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("accepted manifest must be a JSON list")
    written: list[str] = []
    for row in rows:
        source = ROOT / row["extractionPath"]
        data = json.loads(source.read_text(encoding="utf-8"))
        body = str(data.get("body") or "").strip()
        title = str(data.get("title") or "").strip()
        url = str(row["canonicalUrl"])
        if not body or not title:
            raise SystemExit(f"{source}: missing title or body")
        article_id = "crawl-" + hashlib.sha256(url.encode()).hexdigest()
        month = row["publishedDate"][:7]
        filename = f"{article_id}-{slug(title)}.md"
        target = ROOT / "Inputs" / "articles" / month / filename
        if target.exists():
            raise SystemExit(f"refusing to overwrite existing input: {target}")
        outlet = str(data.get("sourceTitle") or row.get("outlet") or "Unknown outlet")
        text = "\n".join([
            "---", f"articleId: {json.dumps(article_id)}", f"articleTitle: {json.dumps(title, ensure_ascii=False)}",
            f"publishedDate: {json.dumps(row['publishedDate'])}", "category: Non-institutional",
            f"topic: {json.dumps(row['topic'], ensure_ascii=False)}", "tone: Factual", "toneSentiment: Neutral",
            "eventType: Unfacilitated", "tags: ['#source']", f"outlets: [{json.dumps(outlet, ensure_ascii=False)}]",
            "countries: []", "coverageCount: 1", "mediaCount: 0", "sourceType: crawl",
            f"url: {json.dumps(url)}", "---", "", body, "",
        ])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        written.append(str(target.relative_to(ROOT)))
    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output_manifest.write_text("\n".join(Path(item).name for item in written) + ("\n" if written else ""), encoding="utf-8")
    print(json.dumps({"materialized": len(written), "manifest": str(args.output_manifest)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
