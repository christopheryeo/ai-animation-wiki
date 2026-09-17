#!/usr/bin/env python3
"""Migrate the historical tag vocabulary and compiled assignments to Tag entities."""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from tag_registry import ROOT, TAG_ROOT, normalize_tag, tag_slug


CSV_SOURCE = ROOT / "runs" / "tag-migration" / "source-tags.csv"
ARTICLE_ROOT = ROOT / "entities" / "article"
DEFAULT_RECEIPT = ROOT / "runs" / "2026-08-18" / "artifacts" / "tag-entity-migration.json"
APPROVED_AT = "2026-08-18T00:00:00+08:00"
APPROVED_BY = "Christopher Yeo"
PROJECTION = re.compile(
    r"(?:^|\n)## Database Projection\s*\n+```json\s*\n(?P<json>.*?)\n```\s*(?=\n## |\Z)",
    re.DOTALL,
)
ISSUE_SECTION = re.compile(
    r"(?:^|\n)## Issue Tags\s*\n(?P<value>.*?)(?=\n## |\Z)",
    re.DOTALL,
)
RADAR_HISTORY = re.compile(
    r"(?:^|\n)## Radar Status History\s*\n(?P<value>.*?)(?=\n## |\Z)", re.DOTALL,
)


class MigrationError(RuntimeError):
    pass


def yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def safe_label(value: str) -> str:
    return re.sub(r"[\[\]|\r\n]+", " ", value).strip() or "Article"


def load_csv_tags() -> set[str]:
    if not CSV_SOURCE.is_file():
        raise MigrationError(f"historical tag CSV not found: {CSV_SOURCE}")
    values: set[str] = set()
    with CSV_SOURCE.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            value = (row.get("source_tag") or "").strip()
            if value:
                values.add(value)
    return values


def article_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(ARTICLE_ROOT.glob("**/*.md")):
        text = path.read_text(encoding="utf-8")
        match = PROJECTION.search(text)
        if not match:
            continue
        projection = json.loads(match.group("json"))
        tags = [str(value).strip() for value in projection.get("tags", []) if str(value).strip()]
        article = projection.get("article") or {}
        published = str(article.get("published_date") or "")
        records.append({
            "path": path,
            "text": text,
            "projection": projection,
            "tags": tags,
            "title": str(article.get("article_title") or path.stem),
            "published": published,
        })
    return records


def assign_ids(values: set[str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for value in values:
        key = normalize_tag(value)
        if key in normalized and normalized[key] != value:
            raise MigrationError(f"normalized-name collision: {normalized[key]!r}, {value!r}")
        normalized[key] = value
    used: dict[str, str] = {}
    output: dict[str, str] = {}
    for value in sorted(values, key=lambda item: (normalize_tag(item), item)):
        base = tag_slug(value)[:180].strip("-") or "tag"
        tag_id = base
        if tag_id in used and used[tag_id] != value:
            suffix = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
            tag_id = f"{base[:169].rstrip('-')}-{suffix}"
        if tag_id in used:
            raise MigrationError(f"unresolved tagId collision: {used[tag_id]!r}, {value!r}")
        used[tag_id] = value
        output[value] = tag_id
    return output


def article_link(record: dict[str, Any]) -> str:
    rel = record["path"].relative_to(ARTICLE_ROOT).with_suffix("")
    return f"[[article/{rel.as_posix()}|{safe_label(record['title'])}]]"


def effective_at(records: list[dict[str, Any]]) -> str:
    dates = sorted(
        value["published"][:10]
        for value in records
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", value["published"])
    )
    date = dates[0] if dates else "2026-08-18"
    return f"{date}T00:00:00+08:00"


def render_tag_note(
    display: str,
    tag_id: str,
    coverage: list[dict[str, Any]],
    csv_tags: set[str],
) -> str:
    unique_records = {str(record["path"]): record for record in coverage}
    ordered = sorted(unique_records.values(), key=lambda item: str(item["path"]))
    effective = effective_at(ordered)
    source = "historical production vocabulary and compiled projections" if display in csv_tags else "existing compiled projections"
    links = "\n".join(f"- {article_link(record)}" for record in ordered) or "- None recorded"
    return f"""---
tagId: {yaml_string(tag_id)}
displayName: {yaml_string(display)}
aliases: []
status: active
statusEffectiveAt: {yaml_string(effective)}
radarStatus: enabled
radarStatusEffectiveAt: {yaml_string(effective)}
approvedAt: {yaml_string(APPROVED_AT)}
approvedBy: {yaml_string(APPROVED_BY)}
articleCount: {len(ordered)}
---

# {display}

## Definition

Canonical issue-radar tag migrated from {source}. Its classification boundary remains the saved
article evidence carrying this tag.

## Replacement

- None — active tag.

## Status History

| Effective At | Status | Actor | Reason |
|---|---|---|---|
| {effective} | active | {APPROVED_BY} | Imported from {source}. |

## Radar Status History

| Effective At | Status | Actor | Reason |
|---|---|---|---|
| {effective} | enabled | {APPROVED_BY} | Initial radar eligibility migration under [[add-tag-radar-eligibility]]. |

## Coverage

{links}

## Notes

- Migrated under [[add-tag-entity-source-of-truth|the Tag source-of-truth decision]].
"""


def render_issue_section(tags: list[str], ids: dict[str, str]) -> str:
    if not tags:
        return "## Issue Tags\n- None recorded"
    return "## Issue Tags\n" + "\n".join(
        f"- [[tag/{ids[value]}|{value}]]" for value in tags
    )


def preserve_radar_governance(rendered: str, existing: str) -> str:
    """Keep approved radar state/history when rebuilding derived Tag coverage."""
    for field in ("radarStatus", "radarStatusEffectiveAt"):
        match = re.search(rf"^{field}:\s*.*$", existing, re.MULTILINE)
        if not match:
            raise MigrationError(f"existing Tag note lacks {field}")
        rendered = re.sub(
            rf"^{field}:\s*.*$", match.group(0), rendered, count=1, flags=re.MULTILINE,
        )
    existing_history = RADAR_HISTORY.search(existing)
    rendered_history = RADAR_HISTORY.search(rendered)
    if not existing_history or not rendered_history:
        raise MigrationError("Tag note lacks Radar Status History")
    replacement = "\n## Radar Status History\n\n" + existing_history.group("value").strip() + "\n"
    return rendered[:rendered_history.start()] + replacement + rendered[rendered_history.end():]


def rewrite_article(text: str, tags: list[str], ids: dict[str, str]) -> str:
    rendered = render_issue_section(tags, ids)
    match = ISSUE_SECTION.search(text)
    if match:
        start = match.start()
        prefix = "\n" if text[start:start + 1] == "\n" else ""
        return text[:start] + prefix + rendered + "\n" + text[match.end():]
    insert = re.search(r"(?:^|\n)## (?:Covered By|Database Projection)\s*\n", text)
    if not insert:
        raise MigrationError("article has no insertion point for ## Issue Tags")
    start = insert.start()
    prefix = "\n" if text[start:start + 1] == "\n" else ""
    return text[:start] + prefix + rendered + "\n" + text[start:]


def append_log(tag_count: int, article_count: int) -> None:
    path = TAG_ROOT / "log.md"
    marker = "Completed the initial Tag-entity migration"
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    entry = (
        f"- 2026-08-18T00:00:00+08:00 - {marker}: created {tag_count} active Tag entities, "
        f"linked {article_count} compiled articles, retired the CSV from operational use, and "
        "preserved Database Projection tag strings unchanged.\n"
    )
    path.write_text(text.rstrip() + "\n" + entry, encoding="utf-8")


def run(write: bool, receipt_path: Path) -> dict[str, Any]:
    csv_tags = load_csv_tags()
    articles = article_records()
    universe = set(csv_tags)
    coverage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in articles:
        for value in record["tags"]:
            universe.add(value)
            coverage[value].append(record)
    ids = assign_ids(universe)
    notes = {
        display: render_tag_note(display, ids[display], coverage[display], csv_tags)
        for display in universe
    }
    for display in universe:
        path = TAG_ROOT / f"{ids[display]}.md"
        if path.exists():
            notes[display] = preserve_radar_governance(
                notes[display], path.read_text(encoding="utf-8")
            )
    article_changes: dict[Path, str] = {}
    for record in articles:
        updated = rewrite_article(record["text"], record["tags"], ids)
        if updated != record["text"]:
            article_changes[record["path"]] = updated
    existing_notes = [path for path in TAG_ROOT.glob("*.md") if path.name not in {"index.md", "catalog.md", "log.md", "_template.md"}]
    unexpected = sorted(path for path in existing_notes if path.stem not in set(ids.values()))
    if unexpected:
        raise MigrationError(f"unexpected existing Tag notes: {[str(p) for p in unexpected[:10]]}")
    note_changes = 0
    for display, content in notes.items():
        path = TAG_ROOT / f"{ids[display]}.md"
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            note_changes += 1
            if write:
                path.write_text(content, encoding="utf-8")
    if write:
        for path, content in article_changes.items():
            path.write_text(content, encoding="utf-8")
        append_log(len(notes), len(articles))
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_catalog.py"), "tag"],
            cwd=ROOT,
            check=True,
        )
    payload = {
        "status": "applied" if write else "preview",
        "csvRows": len(csv_tags),
        "projectionArticles": len(articles),
        "tagEntities": len(notes),
        "projectionOnlyTags": len(universe - csv_tags),
        "tagAssignments": sum(len(record["tags"]) for record in articles),
        "tagNotesChanged": note_changes,
        "articlesChanged": len(article_changes),
        "normalizedCollisions": 0,
        "databaseWrites": 0,
        "productionWrites": 0,
        "historicalCsvSha256": hashlib.sha256(CSV_SOURCE.read_bytes()).hexdigest(),
    }
    if write:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--receipt", type=Path, default=DEFAULT_RECEIPT)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.write, args.receipt.resolve()), indent=2, sort_keys=True))
        return 0
    except (MigrationError, OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
