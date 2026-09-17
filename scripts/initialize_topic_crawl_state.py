#!/usr/bin/env python3
"""Initialize and validate Topic Entity crawl checkpoints and workflow status."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOPIC_ROOT = ROOT / "entities" / "topic"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}
CONFLICTED_COPY_MARKER = "conflicted copy"
ALLOWED_STATUSES = {
    "Not started",
    "Queued",
    "In progress",
    "Completed",
    "Failed",
    "Cancelled",
}
LEGACY_STATUSES = {
    "not-crawled": "Not started",
    "crawl-not-started": "Not started",
    "ready-to-crawl": "Queued",
    "crawl-queued": "Queued",
    "in-progress": "In progress",
    "crawl-in-progress": "In progress",
    "completed": "Completed",
    "crawl-completed": "Completed",
    "failed": "Failed",
    "crawl-failed": "Failed",
    "cancelled": "Cancelled",
    "crawl-cancelled": "Cancelled",
}


class StateError(RuntimeError):
    pass


def topic_paths() -> list[Path]:
    return sorted(
        path
        for path in TOPIC_ROOT.glob("*.md")
        if path.name not in SYSTEM_FILES
        and not path.name.startswith("log-")
        and CONFLICTED_COPY_MARKER not in path.name.lower()
    )


def split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise StateError("note lacks opening frontmatter delimiter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise StateError("note lacks closing frontmatter delimiter")
    return text[4:end], text[end + 5 :]


def timestamp_text(value: object) -> str:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            raise StateError("timestamp lacks timezone")
        return value.isoformat().replace("+00:00", "Z")
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise StateError("timestamp lacks timezone")
    return str(value)


def set_frontmatter_field(raw: str, field: str, rendered_value: str, after: str) -> str:
    pattern = re.compile(rf"(?m)^{re.escape(field)}:\s*.*$")
    matches = list(pattern.finditer(raw))
    if len(matches) > 1:
        raise StateError(f"duplicate {field} fields")
    if matches:
        return pattern.sub(f"{field}: {rendered_value}", raw, count=1)
    anchor = re.search(rf"(?m)^{re.escape(after)}:\s*.*$", raw)
    if not anchor:
        raise StateError(f"lacks {after} anchor for {field}")
    return raw[: anchor.end()] + f"\n{field}: {rendered_value}" + raw[anchor.end() :]


def initialize(dry_run: bool) -> dict[str, int | bool]:
    changed = []
    for path in topic_paths():
        text = path.read_text(encoding="utf-8")
        raw, body = split_frontmatter(text)
        data = yaml.safe_load(raw) or {}
        updated_raw = raw
        if "lastCrawledAt" not in data:
            updated_raw = set_frontmatter_field(updated_raw, "lastCrawledAt", "null", "articleCount")
            last_crawled_at = None
        else:
            last_crawled_at = data["lastCrawledAt"]

        current_status = data.get("crawlStatus")
        normalized_status = LEGACY_STATUSES.get(str(current_status), current_status)
        if normalized_status is None:
            normalized_status = "Completed" if last_crawled_at is not None else "Not started"
        if normalized_status not in ALLOWED_STATUSES:
            raise StateError(f"{path.name}: invalid crawlStatus {normalized_status!r}")
        updated_raw = set_frontmatter_field(
            updated_raw, "crawlStatus", str(normalized_status), "lastCrawledAt"
        )

        current_status_at = data.get("crawlStatusAt")
        if current_status_at is None and normalized_status == "Completed":
            if last_crawled_at is None:
                raise StateError(f"{path.name}: Completed status lacks lastCrawledAt")
            rendered_status_at = timestamp_text(last_crawled_at)
        elif current_status_at is None:
            rendered_status_at = "null"
        else:
            rendered_status_at = timestamp_text(current_status_at)
        updated_raw = set_frontmatter_field(
            updated_raw, "crawlStatusAt", rendered_status_at, "crawlStatus"
        )

        if updated_raw == raw:
            continue
        updated = "---\n" + updated_raw + "\n---\n" + body
        changed.append((path, str(data.get("displayName") or path.stem), str(normalized_status)))
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
    if changed and not dry_run:
        timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with (TOPIC_ROOT / "log.md").open("a", encoding="utf-8") as handle:
            for path, display_name, status in changed:
                handle.write(
                    f"- {timestamp} | source: [[add-topic-crawl-status]] | entity: "
                    f"[[{path.stem}|{display_name}]] | action: updated — initialized "
                    f"`crawlStatus: {status}` and its status time | reasoning: expose the current "
                    "topic-crawl workflow state without changing the latest successful checkpoint.\n"
                )
    return {"topics": len(topic_paths()), "changed": len(changed), "dryRun": dry_run}


def validate() -> dict[str, int]:
    failures = []
    for path in topic_paths():
        raw, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        data = yaml.safe_load(raw) or {}
        if "lastCrawledAt" not in data:
            failures.append(f"{path.name}: missing lastCrawledAt")
            continue
        value = data["lastCrawledAt"]
        if isinstance(value, dt.datetime):
            if value.tzinfo is None:
                failures.append(f"{path.name}: lastCrawledAt lacks timezone")
        elif value is not None:
            try:
                parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    failures.append(f"{path.name}: lastCrawledAt lacks timezone")
            except ValueError:
                failures.append(f"{path.name}: lastCrawledAt is not ISO 8601")
        status = data.get("crawlStatus")
        if status not in ALLOWED_STATUSES:
            failures.append(f"{path.name}: invalid or missing crawlStatus")
        if "crawlStatusAt" not in data:
            failures.append(f"{path.name}: missing crawlStatusAt")
            continue
        status_at = data["crawlStatusAt"]
        if status == "Not started":
            if value is not None:
                failures.append(f"{path.name}: Not started has a successful checkpoint")
            if status_at is not None:
                failures.append(f"{path.name}: Not started has a status timestamp")
        elif status_at is None:
            failures.append(f"{path.name}: {status} lacks crawlStatusAt")
        else:
            try:
                parsed_status_at = dt.datetime.fromisoformat(str(status_at).replace("Z", "+00:00"))
                if parsed_status_at.tzinfo is None:
                    failures.append(f"{path.name}: crawlStatusAt lacks timezone")
            except ValueError:
                failures.append(f"{path.name}: crawlStatusAt is not ISO 8601")
        if status == "Completed" and value is None:
            failures.append(f"{path.name}: Completed lacks lastCrawledAt")
    if failures:
        raise StateError("; ".join(failures[:20]))
    return {
        "topics": len(topic_paths()),
        "validFields": len(topic_paths()),
        "validStatuses": len(topic_paths()),
        "failures": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        result = validate() if args.check else initialize(dry_run=not args.apply)
        print(json.dumps(result, indent=2))
        return 0
    except (StateError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
