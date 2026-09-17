#!/usr/bin/env python3
"""Read and validate the Markdown Tag-entity registry."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
from pathlib import Path
import re
import unicodedata
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
TAG_ROOT = ROOT / "entities" / "tag"
STATUSES = {"awaiting_approval", "active", "inactive", "deprecated", "rejected"}
RADAR_STATUSES = {"enabled", "shadow", "disabled"}
ALLOWED_TRANSITIONS = {
    "awaiting_approval": {"active", "rejected"},
    "active": {"inactive", "deprecated"},
    "inactive": {"active", "deprecated"},
    "rejected": {"awaiting_approval"},
    "deprecated": set(),
}
RADAR_ALLOWED_TRANSITIONS = {
    "enabled": {"shadow"},
    "shadow": {"enabled", "disabled"},
    "disabled": {"shadow"},
}
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}
STATUS_ROW = re.compile(
    r"^\|\s*(?P<effective_at>[^|]+?)\s*\|\s*(?P<status>[^|]+?)\s*\|"
    r"\s*(?P<actor>[^|]+?)\s*\|\s*(?P<reason>[^|]+?)\s*\|$"
)


class TagRegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StatusEvent:
    effective_at: str
    status: str
    actor: str
    reason: str


@dataclass(frozen=True)
class TagRecord:
    path: Path
    tag_id: str
    display_name: str
    aliases: tuple[str, ...]
    status: str
    status_effective_at: str
    radar_status: str
    radar_status_effective_at: str
    approved_at: str | None
    approved_by: str | None
    article_count: int
    status_history: tuple[StatusEvent, ...]
    radar_status_history: tuple[StatusEvent, ...]
    body: str


def normalize_tag(value: str) -> str:
    return " ".join(str(value).strip().casefold().split())


def tag_slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "tag"


def split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        raise TagRegistryError(f"missing YAML frontmatter: {path}")
    end = text.find("\n---", 4)
    if end < 0:
        raise TagRegistryError(f"unterminated YAML frontmatter: {path}")
    value = yaml.safe_load(text[4:end]) or {}
    if not isinstance(value, dict):
        raise TagRegistryError(f"frontmatter must be a mapping: {path}")
    return value, text[end + 4 :].lstrip("\n")


def iso_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value).strip() or None


def section(body: str, heading: str) -> str | None:
    match = re.search(
        rf"(?:^|\n)## {re.escape(heading)}\s*\n(?P<value>.*?)(?=\n## |\Z)",
        body,
        re.DOTALL,
    )
    return match.group("value").strip() if match else None


def parse_history(body: str, path: Path, heading: str) -> tuple[StatusEvent, ...]:
    value = section(body, heading)
    if value is None:
        raise TagRegistryError(f"missing ## {heading}: {path}")
    events: list[StatusEvent] = []
    for line in value.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "Effective At" in stripped or re.match(r"^\|[\s:-]+\|", stripped):
            continue
        match = STATUS_ROW.match(stripped)
        if not match:
            raise TagRegistryError(f"invalid {heading} row in {path}: {stripped}")
        events.append(StatusEvent(**match.groupdict()))
    if not events:
        raise TagRegistryError(f"empty {heading}: {path}")
    return tuple(events)


def parse_status_history(body: str, path: Path) -> tuple[StatusEvent, ...]:
    return parse_history(body, path, "Status History")


def load_tag(path: Path) -> TagRecord:
    frontmatter, body = split_frontmatter(path.read_text(encoding="utf-8"), path)
    aliases = frontmatter.get("aliases") or []
    if not isinstance(aliases, list):
        raise TagRegistryError(f"aliases must be a list: {path}")
    try:
        article_count = int(frontmatter.get("articleCount"))
    except (TypeError, ValueError) as exc:
        raise TagRegistryError(f"articleCount must be an integer: {path}") from exc
    record = TagRecord(
        path=path,
        tag_id=str(frontmatter.get("tagId") or "").strip(),
        display_name=str(frontmatter.get("displayName") or "").strip(),
        aliases=tuple(str(item).strip() for item in aliases if str(item).strip()),
        status=str(frontmatter.get("status") or "").strip(),
        status_effective_at=iso_value(frontmatter.get("statusEffectiveAt")) or "",
        radar_status=str(frontmatter.get("radarStatus") or "").strip(),
        radar_status_effective_at=iso_value(frontmatter.get("radarStatusEffectiveAt")) or "",
        approved_at=iso_value(frontmatter.get("approvedAt")),
        approved_by=iso_value(frontmatter.get("approvedBy")),
        article_count=article_count,
        status_history=parse_status_history(body, path),
        radar_status_history=parse_history(body, path, "Radar Status History"),
        body=body,
    )
    return record


def load_registry(tag_root: Path = TAG_ROOT) -> list[TagRecord]:
    if not tag_root.is_dir():
        raise TagRegistryError(f"Tag entity directory not found: {tag_root}")
    return [
        load_tag(path)
        for path in sorted(tag_root.glob("*.md"))
        if path.name not in SYSTEM_FILES
    ]


def active_inventory(tag_root: Path = TAG_ROOT) -> list[tuple[str, str, int]]:
    """Return the enrichment-compatible inventory from active Tag entities."""

    return [
        (record.display_name, record.display_name.casefold(), record.article_count)
        for record in load_registry(tag_root)
        if record.status == "active"
    ]


def registry_lookup(records: list[TagRecord]) -> dict[str, TagRecord]:
    lookup: dict[str, TagRecord] = {}
    for record in records:
        for value in (record.display_name, *record.aliases):
            key = normalize_tag(value)
            if key in lookup and lookup[key].tag_id != record.tag_id:
                raise TagRegistryError(
                    f"duplicate canonical name or alias {value!r}: "
                    f"{lookup[key].tag_id}, {record.tag_id}"
                )
            lookup[key] = record
    return lookup


def status_at(record: TagRecord, as_of: dt.date) -> str | None:
    return history_value_at(record.status_history, as_of, record.tag_id, "status")


def radar_status_at(record: TagRecord, as_of: dt.date) -> str | None:
    return history_value_at(record.radar_status_history, as_of, record.tag_id, "radar status")


def history_value_at(
    events: tuple[StatusEvent, ...], as_of: dt.date, tag_id: str, label: str,
) -> str | None:
    applicable: list[tuple[dt.datetime, str]] = []
    for event in events:
        try:
            parsed = dt.datetime.fromisoformat(event.effective_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise TagRegistryError(
                f"invalid {label}-history timestamp for {tag_id}: {event.effective_at}"
            ) from exc
        if parsed.date() <= as_of:
            applicable.append((parsed, event.status))
    return max(applicable, default=(None, None), key=lambda item: item[0] or dt.datetime.min)[1]
