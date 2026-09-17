#!/usr/bin/env python3
"""Document same-source arrivals held because their compiled article exists.

This is a read-only evidence audit apart from writing its JSON receipt.  It
matches each held filename's leading article ID to exactly one compiled note
and proves whether the saved canonical URL is identical in both copies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from enrich_radar_inputs import parse_frontmatter, split_note


ROOT = Path(__file__).resolve().parents[1]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metadata(path: Path) -> dict:
    lines, _ = split_note(path.read_text(encoding="utf-8"))
    return parse_frontmatter(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hold-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    hold_dir = args.hold_dir.resolve()
    rows = []
    errors = []
    for held in sorted(hold_dir.glob("*.md")):
        article_id = held.name.split("-", 1)[0]
        matches = sorted((ROOT / "entities" / "article").glob(f"*/{article_id}-*.md"))
        if len(matches) != 1:
            errors.append({
                "heldPath": str(held.relative_to(ROOT)),
                "articleId": article_id,
                "error": f"expected one compiled match, found {len(matches)}",
            })
            continue
        compiled = matches[0]
        held_meta = metadata(held)
        compiled_meta = metadata(compiled)
        held_url = str(held_meta.get("url") or held_meta.get("sourceUrl") or "").strip()
        compiled_url = str(compiled_meta.get("sourceUrl") or compiled_meta.get("url") or "").strip()
        rows.append({
            "heldPath": str(held.relative_to(ROOT)),
            "compiledPath": str(compiled.relative_to(ROOT)),
            "articleId": article_id,
            "publisherDomain": str(held_meta.get("publisherDomain") or ""),
            "heldUrl": held_url,
            "compiledUrl": compiled_url,
            "exactUrlMatch": bool(held_url) and held_url == compiled_url,
            "heldSha256": sha256(held),
            "disposition": "verified same-outlet repeat arrival; held unchanged",
        })

    payload = {
        "schemaVersion": 1,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "holdCount": len(rows),
        "allExactUrlMatches": bool(rows) and all(row["exactUrlMatch"] for row in rows),
        "errorCount": len(errors),
        "holds": rows,
        "errors": errors,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "holdCount": len(rows),
        "allExactUrlMatches": payload["allExactUrlMatches"],
        "errorCount": len(errors),
        "output": str(args.output),
    }, indent=2))
    return 0 if rows and payload["allExactUrlMatches"] and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
