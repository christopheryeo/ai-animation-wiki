#!/usr/bin/env python3
"""Mechanically merge one bad generated outlet identity into a canonical outlet.

The repair is deliberately narrow: one compiled article, one old outlet note,
and one existing canonical outlet note.  It preserves the bad note in the run
artifact directory, rewrites the article backlink and projection coverage
label, and adds the article to the canonical outlet's Coverage list.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--article", type=Path, required=True)
    parser.add_argument("--old-outlet", required=True)
    parser.add_argument("--new-outlet", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    args = parser.parse_args()

    article = args.article.resolve()
    old_note = ROOT / "entities" / "outlet" / f"{args.old_outlet}.md"
    new_note = ROOT / "entities" / "outlet" / f"{args.new_outlet}.md"
    if not article.is_file() or not old_note.is_file() or not new_note.is_file():
        raise SystemExit("article, old outlet, and canonical outlet must all exist")

    text = article.read_text(encoding="utf-8")
    old_link = re.compile(
        rf"\[\[outlet/{re.escape(args.old_outlet)}\|[^\]]+\]\]"
    )
    text, link_changes = old_link.subn(
        f"[[outlet/{args.new_outlet}|{args.new_outlet}]]", text
    )
    if link_changes != 1:
        raise SystemExit(f"expected one old outlet wikilink, found {link_changes}")

    projection_match = re.search(
        r"(## Database Projection\n```json\n)(\{.*?\})(\n```)",
        text,
        flags=re.DOTALL,
    )
    if not projection_match:
        raise SystemExit("Database Projection JSON not found")
    projection = json.loads(projection_match.group(2))
    coverage = projection.get("coverage")
    if not isinstance(coverage, list) or len(coverage) != 1:
        raise SystemExit("expected exactly one coverage projection row")
    coverage[0]["display_name"] = args.new_outlet
    replacement = (
        projection_match.group(1)
        + json.dumps(projection, ensure_ascii=False, separators=(",", ":"))
        + projection_match.group(3)
    )
    text = text[: projection_match.start()] + replacement + text[projection_match.end() :]
    article.write_text(text, encoding="utf-8")

    old_text = old_note.read_text(encoding="utf-8")
    coverage_line = next(
        (line for line in old_text.splitlines() if line.startswith("- [[article/")),
        None,
    )
    if not coverage_line:
        raise SystemExit("old outlet has no article coverage line")
    canonical = new_note.read_text(encoding="utf-8")
    if coverage_line not in canonical:
        canonical = canonical.rstrip() + "\n" + coverage_line + "\n"
    coverage_count = sum(
        1 for line in canonical.splitlines() if line.startswith("- [[article/")
    )
    canonical = re.sub(
        r"(?m)^articleCount: \d+$", f"articleCount: {coverage_count}", canonical
    )
    new_note.write_text(canonical, encoding="utf-8")

    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = args.backup_dir / old_note.name
    if backup.exists():
        raise SystemExit(f"backup already exists: {backup}")
    shutil.move(old_note, backup)
    print(json.dumps({
        "article": str(article.relative_to(ROOT)),
        "oldOutletBackup": str(backup),
        "canonicalOutlet": str(new_note.relative_to(ROOT)),
        "canonicalCoverageCount": coverage_count,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
