#!/usr/bin/env python3
"""Exclude already-compiled source IDs from a frozen topic-crawl manifest."""
from __future__ import annotations
import argparse, json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def source_id(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^(?:articleId|sourceId):\s*[\"']?([^\"'\n]+)", text, re.M)
    return match.group(1) if match else ""

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    compiled = {source_id(path): path for path in (ROOT / "entities" / "article" / args.month).glob("*.md")}
    accepted, duplicates = [], []
    for name in args.manifest.read_text(encoding="utf-8").splitlines():
        if not name.strip(): continue
        raw = ROOT / "Inputs" / "articles" / args.month / name
        found = compiled.get(source_id(raw))
        if found:
            duplicates.append({"input": str(raw.relative_to(ROOT)), "compiled": str(found.relative_to(ROOT)), "sourceId": source_id(raw)})
        else:
            accepted.append(name)
    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output_manifest.write_text("\n".join(accepted) + ("\n" if accepted else ""), encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps({"accepted": len(accepted), "duplicates": duplicates}, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"accepted": len(accepted), "duplicates": len(duplicates)}))

if __name__ == "__main__": main()
