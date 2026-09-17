#!/usr/bin/env python3
"""Validate the frozen Tag entity registry and optional article assignments."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys

from tag_registry import (
    ALLOWED_TRANSITIONS,
    RADAR_ALLOWED_TRANSITIONS,
    RADAR_STATUSES,
    ROOT,
    STATUSES,
    TAG_ROOT,
    TagRecord,
    TagRegistryError,
    load_registry,
    normalize_tag,
    registry_lookup,
    section,
)


PROJECTION = re.compile(
    r"(?:^|\n)## Database Projection\s*\n+```json\s*\n(?P<json>.*?)\n```\s*(?=\n## |\Z)",
    re.DOTALL,
)
ISSUE_LINK = re.compile(r"^- \[\[tag/(?P<id>[^\]|]+)\|(?P<display>[^\]]+)\]\]$", re.MULTILINE)
REQUIRED_HEADINGS = (
    "Definition", "Replacement", "Status History", "Radar Status History", "Coverage", "Notes",
)
EXPECTED_FIELDS = {
    "tagId", "displayName", "aliases", "status", "statusEffectiveAt",
    "radarStatus", "radarStatusEffectiveAt", "approvedAt", "approvedBy", "articleCount",
}


def frontmatter_fields(record: TagRecord) -> set[str]:
    text = record.path.read_text(encoding="utf-8")
    end = text.find("\n---", 4)
    import yaml
    return set((yaml.safe_load(text[4:end]) or {}).keys())


def validate_record(record: TagRecord) -> list[str]:
    errors: list[str] = []
    rel = record.path.relative_to(ROOT)
    fields = frontmatter_fields(record)
    if fields != EXPECTED_FIELDS:
        errors.append(f"{rel}: field set differs; missing={sorted(EXPECTED_FIELDS-fields)} extra={sorted(fields-EXPECTED_FIELDS)}")
    if record.path.stem != record.tag_id or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record.tag_id):
        errors.append(f"{rel}: filename/tagId is invalid")
    if not record.display_name or len(record.display_name) > 200:
        errors.append(f"{rel}: displayName must contain 1-200 characters")
    if record.status not in STATUSES:
        errors.append(f"{rel}: invalid status {record.status!r}")
    if not record.status_effective_at:
        errors.append(f"{rel}: statusEffectiveAt is missing")
    if record.radar_status not in RADAR_STATUSES:
        errors.append(f"{rel}: invalid radarStatus {record.radar_status!r}")
    if not record.radar_status_effective_at:
        errors.append(f"{rel}: radarStatusEffectiveAt is missing")
    if record.status in {"active", "inactive", "deprecated"} and (not record.approved_at or not record.approved_by):
        errors.append(f"{rel}: approvedAt and approvedBy are required after activation")
    if record.article_count < 0:
        errors.append(f"{rel}: articleCount is negative")
    for heading in REQUIRED_HEADINGS:
        if section(record.body, heading) is None:
            errors.append(f"{rel}: missing ## {heading}")
    replacement = section(record.body, "Replacement") or ""
    if record.status == "deprecated" and "None — active tag" in replacement:
        errors.append(f"{rel}: deprecated tag lacks replacement or retirement reason")
    previous = None
    for event in record.status_history:
        if event.status not in STATUSES:
            errors.append(f"{rel}: invalid history status {event.status!r}")
        if not event.actor or not event.reason:
            errors.append(f"{rel}: history row lacks actor or reason")
        if previous and event.status not in ALLOWED_TRANSITIONS.get(previous, set()):
            errors.append(f"{rel}: forbidden transition {previous}->{event.status}")
        previous = event.status
    if record.status_history[-1].status != record.status:
        errors.append(f"{rel}: current status differs from final history row")
    if record.status_history[-1].effective_at != record.status_effective_at:
        errors.append(f"{rel}: statusEffectiveAt differs from final history row")
    radar_previous = None
    for event in record.radar_status_history:
        if event.status not in RADAR_STATUSES:
            errors.append(f"{rel}: invalid radar history state {event.status!r}")
        if not event.actor or not event.reason:
            errors.append(f"{rel}: radar history row lacks actor or reason")
        if radar_previous and event.status not in RADAR_ALLOWED_TRANSITIONS.get(radar_previous, set()):
            errors.append(f"{rel}: forbidden radar transition {radar_previous}->{event.status}")
        radar_previous = event.status
    if record.radar_status_history[-1].status != record.radar_status:
        errors.append(f"{rel}: current radarStatus differs from final radar history row")
    if record.radar_status_history[-1].effective_at != record.radar_status_effective_at:
        errors.append(f"{rel}: radarStatusEffectiveAt differs from final radar history row")
    coverage = section(record.body, "Coverage") or ""
    links = re.findall(r"^- \[\[([^\]|]+)\|[^\]]+\]\]$", coverage, re.MULTILINE)
    if len(links) != len(set(links)):
        errors.append(f"{rel}: duplicate Coverage links")
    if len(links) != record.article_count:
        errors.append(f"{rel}: articleCount={record.article_count} coverage={len(links)}")
    return errors


def validate_articles(records: list[TagRecord]) -> tuple[list[str], dict[str, int]]:
    errors: list[str] = []
    by_display = {record.display_name: record for record in records}
    expected_counts: Counter[str] = Counter()
    checked = 0
    for path in sorted((ROOT / "entities" / "article").glob("**/*.md")):
        text = path.read_text(encoding="utf-8")
        match = PROJECTION.search(text)
        if not match:
            continue
        checked += 1
        projection = json.loads(match.group("json"))
        projected = [str(value).strip() for value in projection.get("tags", []) if str(value).strip()]
        for value in set(projected):
            record = by_display.get(value)
            if record is None:
                errors.append(f"{path.relative_to(ROOT)}: unknown projected tag {value!r}")
            elif record.status != "active":
                errors.append(f"{path.relative_to(ROOT)}: projected tag is not active {value!r}")
            else:
                expected_counts[record.tag_id] += 1
        issue_match = re.search(r"(?:^|\n)## Issue Tags\s*\n(?P<value>.*?)(?=\n## |\Z)", text, re.DOTALL)
        if issue_match:
            links = ISSUE_LINK.findall(issue_match.group("value"))
            linked = [display for _, display in links]
            if Counter(linked) != Counter(projected):
                errors.append(f"{path.relative_to(ROOT)}: Issue Tags links differ from projection tags")
            for tag_id, display in links:
                record = by_display.get(display)
                if record is None or record.tag_id != tag_id:
                    errors.append(f"{path.relative_to(ROOT)}: invalid Tag link {tag_id}|{display}")
        elif projected:
            errors.append(f"{path.relative_to(ROOT)}: missing ## Issue Tags")
    return errors, {"articlesChecked": checked, "assignedTags": sum(expected_counts.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles", action="store_true", help="also validate compiled article assignments")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    try:
        records = load_registry(TAG_ROOT)
        errors: list[str] = []
        seen_ids: set[str] = set()
        seen_names: dict[str, str] = {}
        for record in records:
            errors.extend(validate_record(record))
            if record.tag_id in seen_ids:
                errors.append(f"duplicate tagId: {record.tag_id}")
            seen_ids.add(record.tag_id)
            for value in (record.display_name, *record.aliases):
                key = normalize_tag(value)
                if key in seen_names and seen_names[key] != record.tag_id:
                    errors.append(f"duplicate name/alias {value!r}: {seen_names[key]}, {record.tag_id}")
                seen_names[key] = record.tag_id
        registry_lookup(records)
        metrics = {
            "tagCount": len(records),
            "activeCount": sum(r.status == "active" for r in records),
            "radarStatusCounts": dict(sorted(Counter(r.radar_status for r in records).items())),
        }
        if args.articles:
            article_errors, article_metrics = validate_articles(records)
            errors.extend(article_errors)
            metrics.update(article_metrics)
        payload = {"status": "passed" if not errors else "failed", **metrics, "errorCount": len(errors), "errors": errors[:200]}
        if args.json_output:
            args.json_output.parent.mkdir(parents=True, exist_ok=True)
            args.json_output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 1 if errors else 0
    except (OSError, ValueError, json.JSONDecodeError, TagRegistryError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
