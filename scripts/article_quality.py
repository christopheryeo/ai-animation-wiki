#!/usr/bin/env python3
"""Validate and safely repair compiled article-note metadata.

Markdown remains the source of truth. This tool checks the frozen registry in
entities/article/index.md and changes only scalar frontmatter fields whose
replacement is supported by preserved source evidence or a supplied reviewed
sentiment file. Bodies and filenames are never rewritten.

Examples:
  python3 scripts/article_quality.py --check
  python3 scripts/article_quality.py --fix-safe --sentiment-overrides review.json --dry-run
  python3 scripts/article_quality.py --fix-safe --sentiment-overrides review.json
  python3 scripts/article_quality.py --check --path entities/article/2026-07/example.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from run_logger import RunLogger

try:
    import yaml
except ImportError:
    print("This script requires PyYAML. Install it with: pip3 install pyyaml", file=sys.stderr)
    raise SystemExit(2)


ROOT = Path(__file__).resolve().parents[1]
ARTICLE_DIR = ROOT / "entities" / "article"
RAW_DIR = ROOT / "raw" / "feed data"
ARTICLE_LOG = ARTICLE_DIR / "log.md"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}

REGISTRY_FIELDS = {
    "type", "subtype", "domain", "status", "aliases", "owner", "created",
    "last_updated", "sourceId", "sourceUrl", "sourceType", "publishedDate",
    "tone", "toneSentiment", "eventType", "coverageCount", "tags",
}
REQUIRED_VALUES = REGISTRY_FIELDS - {"aliases", "sourceUrl"}
ENUMS = {
    "status": {"active", "superseded", "contested"},
    "sourceType": {"feed", "crawl"},
    "tone": {"Factual", "Opinionated"},
    "toneSentiment": {"Positive", "Neutral", "Negative"},
    "eventType": {"Facilitated", "Unfacilitated"},
}
FIXED = {"type": "source", "subtype": "article", "domain": "Sources"}
CORE_SECTIONS = ("Summary", "Key Points", "Covered By", "AI Context")
UUID_PREFIX = re.compile(
    r"^([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:-|$)"
)
# Crawler-issued hash IDs, e.g. `art-ff4d3a6b-...` or `crawl-fff1...-...` — an
# alphabetic tag followed by a short hex hash, distinct from the numeric feed
# ID and full-UUID conventions above. Registered by
# [[normalise-news-sourcetype-and-art-hash-ids]].
HASH_ID_PREFIX = re.compile(r"^([a-z]+-[0-9a-fA-F]{6,12})(?:-|$)")


def split_note(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        raise ValueError("missing opening frontmatter delimiter")
    end = text.find("\n---", 4)
    if end == -1:
        raise ValueError("missing closing frontmatter delimiter")
    return text[4:end], text[end + 4 :]


def article_paths(explicit: list[str]) -> list[Path]:
    if explicit:
        return sorted(Path(value).resolve() for value in explicit)
    return sorted(
        path for path in ARTICLE_DIR.glob("*/*.md")
        if path.name not in SYSTEM_FILES
    )


def expected_source_id(path: Path, current: str) -> str:
    stem = path.stem
    # Crawler IDs may themselves contain hyphens (for example
    # 2026-08-1251622954). Preserve the registered ID when the filename begins
    # with that exact ID before applying legacy numeric-prefix inference.
    if current and (stem == current or stem.startswith(current + "-")):
        return current
    numeric = re.match(r"^(\d+)-", stem)
    if numeric:
        return numeric.group(1)
    uuid = UUID_PREFIX.match(stem)
    if uuid:
        return uuid.group(1)
    hashed = HASH_ID_PREFIX.match(stem)
    if hashed:
        return hashed.group(1)
    return stem.split("-", 1)[0]


def load_raw_records() -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(RAW_DIR.glob("*_feed_data.txt")):
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        for record in payload:
            article_id = record.get("articleId")
            if article_id is not None:
                records[str(article_id)] = record
    return records


def replace_scalar(frontmatter: str, key: str, rendered_value: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*.*$", re.MULTILINE)
    if not pattern.search(frontmatter):
        raise ValueError(f"cannot repair missing field {key}")
    return pattern.sub(f"{key}: {rendered_value}", frontmatter, count=1)


def parse_datetime(value: Any) -> bool:
    if not value:
        return False
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def finding(code: str, path: Path, message: str, severity: str = "error") -> dict[str, str]:
    try:
        rel = str(path.relative_to(ROOT))
    except ValueError:
        rel = str(path)
    return {"severity": severity, "code": code, "path": rel, "message": message}


def validate_note(path: Path, data: dict[str, Any], body: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    unknown = sorted(set(data) - REGISTRY_FIELDS)
    missing = sorted(REGISTRY_FIELDS - set(data))
    if unknown:
        out.append(finding("unknown-fields", path, ", ".join(unknown)))
    if missing:
        out.append(finding("missing-fields", path, ", ".join(missing)))
    for key in sorted(REQUIRED_VALUES):
        if data.get(key) in (None, "", [], {}):
            out.append(finding("missing-value", path, key))
    for key, expected in FIXED.items():
        if data.get(key) != expected:
            out.append(finding("fixed-value", path, f"{key}={data.get(key)!r}; expected {expected!r}"))
    for key, allowed in ENUMS.items():
        if data.get(key) not in allowed:
            out.append(finding("invalid-enum", path, f"{key}={data.get(key)!r}"))
    source_id = str(data.get("sourceId") or "")
    expected_id = expected_source_id(path, source_id)
    if source_id != expected_id:
        out.append(finding("source-id-filename", path, f"{source_id!r}; expected {expected_id!r}"))
    if not isinstance(data.get("aliases"), list):
        out.append(finding("invalid-type", path, "aliases must be a list"))
    tags = data.get("tags")
    if not isinstance(tags, list):
        out.append(finding("invalid-type", path, "tags must be a list"))
    elif "#source" not in tags:
        out.append(finding("missing-source-tag", path, "tags must contain #source"))
    coverage = data.get("coverageCount")
    if not isinstance(coverage, int) or isinstance(coverage, bool) or coverage < 0:
        out.append(finding("invalid-type", path, "coverageCount must be a non-negative integer"))
    published = data.get("publishedDate")
    if not parse_datetime(published):
        out.append(finding("invalid-datetime", path, f"publishedDate={published!r}"))
    elif re.fullmatch(r"\d{4}-\d{2}", path.parent.name) and str(published)[:7] != path.parent.name:
        out.append(finding("month-placement", path, f"publishedDate {published!r} is outside {path.parent.name}"))
    for section in CORE_SECTIONS:
        if not re.search(rf"^## {re.escape(section)}\s*$", body, re.MULTILINE):
            out.append(finding("missing-section", path, section))
    if not re.search(r"^## Related Entities\s*$", body, re.MULTILINE):
        out.append(finding("missing-related-section", path, "Related Entities", severity="warning"))
    return out


def load_sentiment_overrides(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        values = json.load(handle)
    bad = {key: value for key, value in values.items() if value not in ENUMS["toneSentiment"]}
    if bad:
        raise ValueError(f"invalid reviewed sentiments: {bad}")
    return {str(key): value for key, value in values.items()}


def append_article_log(changes: list[dict[str, Any]], timestamp: str) -> None:
    if not changes:
        return
    lines = []
    for change in changes:
        path: Path = change["path"]
        rel_no_ext = path.relative_to(ROOT / "entities").with_suffix("")
        details = "; ".join(change["details"])
        lines.append(
            f"- {timestamp} | source: article metadata audit | entity: [[{rel_no_ext}]] | "
            f"action: updated — {details} | reasoning: deterministic source-backed repair or "
            f"reviewed sentiment correction authorized by [[enforce-article-quality-gates]]."
        )
    with ARTICLE_LOG.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate only (the default).")
    parser.add_argument("--fix-safe", action="store_true", help="Apply provenance-backed ID/source-type repairs.")
    parser.add_argument("--sentiment-overrides", help="Reviewed JSON mapping of sourceId to sentiment.")
    parser.add_argument("--dry-run", action="store_true", help="Preview repairs without writing.")
    parser.add_argument("--path", action="append", default=[], help="Validate one compiled note; repeatable.")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
    parser.add_argument("--limit", type=int, default=30, help="Maximum detailed findings to print.")
    parser.add_argument("--no-run-log", action="store_true")
    args = parser.parse_args()

    if args.check and (args.fix_safe or args.sentiment_overrides):
        parser.error("--check cannot be combined with repair options")
    do_repair = bool(args.fix_safe or args.sentiment_overrides)
    paths = article_paths(args.path)
    overrides = load_sentiment_overrides(args.sentiment_overrides)
    raw = load_raw_records() if args.fix_safe else {}
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    changes: list[dict[str, Any]] = []
    findings: list[dict[str, str]] = []
    seen: dict[str, Path] = {}

    run = None if args.no_run_log else RunLogger(
        "lint",
        metadata={"script": "article_quality.py", "repair": do_repair, "dryRun": args.dry_run},
        notes="Article schema validation and source-backed metadata repair.",
    )
    try:
        for path in paths:
            try:
                original = path.read_text(encoding="utf-8")
                frontmatter, body = split_note(original)
                data = yaml.safe_load(frontmatter) or {}
                if not isinstance(data, dict):
                    raise ValueError("frontmatter must parse to a mapping")
            except Exception as exc:  # noqa: BLE001 - report all invalid notes in one run
                findings.append(finding("yaml-error", path, str(exc)))
                continue

            details: list[str] = []
            source_id = str(data.get("sourceId") or "")
            expected_id = expected_source_id(path, source_id)
            record = raw.get(expected_id)

            if args.fix_safe and source_id != expected_id:
                # A hash-style ID (art-<hash>, crawl-<hash>) has no raw feed-JSON
                # record to confirm against. It is instead self-confirming: the
                # filename was generated once, deterministically, from this same
                # ID at compile time, so if the current sourceId differs from the
                # filename-derived ID only by separator style or case, the
                # filename itself is the provenance for the correction — no new
                # fact is invented. Registered by
                # [[normalise-news-sourcetype-and-art-hash-ids]].
                hash_confirmed = bool(
                    HASH_ID_PREFIX.match(expected_id)
                    and re.sub(r"[^0-9a-zA-Z]", "-", source_id).lower() == expected_id.lower()
                )
                if record and str(record.get("articleId")) == expected_id:
                    frontmatter = replace_scalar(frontmatter, "sourceId", repr(expected_id))
                    data["sourceId"] = expected_id
                    details.append(f"sourceId {source_id!r} -> {expected_id!r}")
                    source_id = expected_id
                elif hash_confirmed:
                    frontmatter = replace_scalar(frontmatter, "sourceId", repr(expected_id))
                    data["sourceId"] = expected_id
                    details.append(f"sourceId {source_id!r} -> {expected_id!r} (filename-confirmed hash ID)")
                    source_id = expected_id
                else:
                    findings.append(finding("unsafe-source-id", path, f"no raw record confirms {expected_id!r}"))

            source_type = data.get("sourceType")
            if args.fix_safe and source_type == "article":
                if record and str(record.get("productType") or "").upper() == "FEED":
                    frontmatter = replace_scalar(frontmatter, "sourceType", "feed")
                    data["sourceType"] = "feed"
                    details.append("sourceType 'article' -> 'feed'")
                else:
                    findings.append(finding("unsafe-source-type", path, "raw record does not confirm FEED"))
            elif args.fix_safe and source_type == "news":
                if data.get("sourceUrl") and record is None:
                    frontmatter = replace_scalar(frontmatter, "sourceType", "crawl")
                    data["sourceType"] = "crawl"
                    details.append("sourceType 'news' -> 'crawl'")
                else:
                    findings.append(finding("unsafe-source-type", path, "crawl provenance is not conclusive"))

            reviewed = overrides.get(source_id)
            if reviewed and data.get("toneSentiment") != reviewed:
                old = data.get("toneSentiment")
                frontmatter = replace_scalar(frontmatter, "toneSentiment", reviewed)
                data["toneSentiment"] = reviewed
                details.append(f"toneSentiment {old!r} -> {reviewed!r} (reviewed)")

            if details:
                frontmatter = replace_scalar(frontmatter, "last_updated", timestamp)
                data["last_updated"] = timestamp
                changes.append({"path": path, "details": details})
                if not args.dry_run:
                    path.write_text("---\n" + frontmatter + "\n---" + body, encoding="utf-8")

            findings.extend(validate_note(path, data, body))
            final_id = str(data.get("sourceId") or "")
            if final_id in seen:
                findings.append(finding("duplicate-source-id", path, f"also used by {seen[final_id].relative_to(ROOT)}"))
            else:
                seen[final_id] = path

        if changes and not args.dry_run:
            append_article_log(changes, timestamp)

        errors = [item for item in findings if item["severity"] == "error"]
        warnings = [item for item in findings if item["severity"] == "warning"]
        summary = {
            "scanned": len(paths), "changed": len(changes), "errors": len(errors),
            "warnings": len(warnings), "dryRun": args.dry_run,
            "findingCounts": dict(Counter(item["code"] for item in findings)),
        }
        if args.json:
            print(json.dumps({"summary": summary, "findings": findings}, indent=2, ensure_ascii=False))
        else:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            for item in findings[: args.limit]:
                print(f"{item['severity'].upper()} {item['code']} {item['path']}: {item['message']}")
            if len(findings) > args.limit:
                print(f"... and {len(findings) - args.limit} more findings")

        if run:
            run.set_article_metrics(
                inputCount=len(paths), processedCount=len(paths), updatedCount=0 if args.dry_run else len(changes),
                failedCount=len(errors), skippedCount=len(paths) - len(changes),
            )
            run.set_file_metrics(scannedCount=len(paths), changedCount=0 if args.dry_run else len(changes), failedCount=len(errors))
            run.add_output("summary", summary)
            if errors:
                run.add_error("Article quality validation found errors", count=len(errors))
    finally:
        if run:
            run.finish()
    return 1 if any(item["severity"] == "error" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
