#!/usr/bin/env python3
"""Add the initial enabled radar status to existing Tag entities idempotently."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from tag_registry import SYSTEM_FILES, TAG_ROOT


ACTOR = "Christopher Yeo"
FIELD_ANCHOR = re.compile(r"^(statusEffectiveAt:\s*.*)$", re.MULTILINE)
SECTION_ANCHOR = re.compile(r"(?=\n## Coverage\s*\n)")
STATUS_EFFECTIVE = re.compile(r'^statusEffectiveAt:\s*["\']?(?P<value>[^"\'\n]+)', re.MULTILINE)


def migrate_text(text: str) -> str:
    updated = text
    match = STATUS_EFFECTIVE.search(updated)
    if not match:
        raise ValueError("Tag note lacks statusEffectiveAt")
    effective_at = match.group("value").strip()
    if not re.search(r"^radarStatus:", updated, re.MULTILINE):
        updated = FIELD_ANCHOR.sub(
            rf'\1\nradarStatus: enabled\nradarStatusEffectiveAt: "{effective_at}"',
            updated,
            count=1,
        )
    if "## Radar Status History" not in updated:
        section = (
            "\n## Radar Status History\n\n"
            "| Effective At | Status | Actor | Reason |\n"
            "|---|---|---|---|\n"
            f"| {effective_at} | enabled | {ACTOR} | Initial radar eligibility migration under "
            "[[add-tag-radar-eligibility]]. |\n"
        )
        updated = SECTION_ANCHOR.sub(section, updated, count=1)
    elif (
        "Initial radar eligibility migration under [[add-tag-radar-eligibility]]" in updated
        and updated.count("| enabled |") == 1
    ):
        updated = re.sub(
            r'^radarStatusEffectiveAt:\s*.*$',
            f'radarStatusEffectiveAt: "{effective_at}"',
            updated,
            count=1,
            flags=re.MULTILINE,
        )
        updated = re.sub(
            r'^\|\s*[^|]+\s*\|\s*enabled\s*\|\s*Christopher Yeo\s*\|\s*Initial radar eligibility migration under \[\[add-tag-radar-eligibility\]\]\.\s*\|$',
            f"| {effective_at} | enabled | {ACTOR} | Initial radar eligibility migration under [[add-tag-radar-eligibility]]. |",
            updated,
            count=1,
            flags=re.MULTILINE,
        )
    return updated


def run(write: bool) -> dict[str, int | str]:
    changed: dict[Path, str] = {}
    paths = [
        path for path in sorted(TAG_ROOT.glob("*.md")) if path.name not in SYSTEM_FILES
    ]
    for path in paths:
        original = path.read_text(encoding="utf-8")
        updated = migrate_text(original)
        if updated != original:
            changed[path] = updated
    if write:
        for path, content in changed.items():
            path.write_text(content, encoding="utf-8")
    return {
        "status": "applied" if write else "preview",
        "tagCount": len(paths),
        "changedCount": len(changed),
        "databaseWrites": 0,
        "productionWrites": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.write), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
