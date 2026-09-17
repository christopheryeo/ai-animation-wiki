#!/usr/bin/env python3
"""Derive raw JSON-style feed exports into Inputs/articles intake notes.

The raw/ folder is preserved source evidence. This script reads feed export
files from raw/feed data/ and writes one Markdown intake note per article under
Inputs/articles/YYYY-MM/. It does not compile notes into entities/article/ and
does not cascade entity links.

The generated files are deliberately the *raw intake* schema described in the
README: YAML frontmatter plus the source narrative body, with no wikilinks and
no compiled article sections. The later compile/cascade process is responsible
for turning these notes into wiki source notes and moving them onward.

Default mode is a dry run. Pass --write to create files.

Examples:
  python3 scripts/raw_feed_to_inputs.py --month 2026-05
  python3 scripts/raw_feed_to_inputs.py "raw/feed data/2026_apr_feed_data.txt" --write
  python3 scripts/raw_feed_to_inputs.py --all --write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
RAW_FEED_DIR = ROOT / "raw" / "feed data"
INPUTS_ARTICLE_DIR = ROOT / "Inputs" / "articles"
ENTITIES_ARTICLE_DIR = ROOT / "entities" / "article"


@dataclass(frozen=True)
class Target:
    """The canonical destination name for one raw feed article.

    A target has two meaningful locations in this vault:
    - Inputs/articles/YYYY-MM/ while the article is pending ingest.
    - entities/article/YYYY-MM/ after compile/cascade has moved it onward.

    The importer checks both locations so reruns stay idempotent even after a
    later ingest process has emptied the corresponding Inputs month folder.
    """

    month: str
    filename: str

    @property
    def input_path(self) -> Path:
        return INPUTS_ARTICLE_DIR / self.month / self.filename

    @property
    def entity_path(self) -> Path:
        return ENTITIES_ARTICLE_DIR / self.month / self.filename


def slugify(text: Any, max_len: int = 60) -> str:
    """Return the filename slug used by the prior one-off imports.

    The 60-character cap is intentionally preserved because it was used when
    deriving the existing April-July targets. Changing it would make reruns
    produce different filenames and break duplicate detection.
    """

    value = str(text) if text is not None else "untitled"
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value[:max_len].rstrip("-") or "untitled"


def yaml_quote(value: Any) -> str:
    """Render a scalar for this vault's YAML frontmatter.

    Single quotes are used for almost every scalar because raw titles and topics
    often contain colons, curly quotes, hashtags, or other punctuation that can
    surprise YAML parsers when left bare. YAML escapes a literal single quote by
    doubling it, which is what this function does.
    """

    if value is None:
        return "null"
    return "'" + str(value).replace("'", "''") + "'"


def yaml_flow_list(values: list[Any]) -> str:
    """Render a de-duplicated YAML flow list with every item quoted.

    Quoting list items is especially important for tags beginning with "#";
    unquoted hash-prefixed values are comments in YAML, which can corrupt the
    frontmatter block during later ingest or catalog generation.
    """

    seen: set[str] = set()
    clean: list[str] = []
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        clean.append(yaml_quote(text))
    return "[" + ", ".join(clean) + "]"


def coverage_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten online, broadcast, and print coverage into one ordered list.

    The raw export stores outlet appearances by medium. The intake schema only
    needs aggregate outlet/country fields plus a coverage count, so this helper
    keeps the flattening rule in one place.
    """

    items: list[dict[str, Any]] = []
    for key in (
        "listOfCoverageOnline",
        "listOfCoverageBroadcast",
        "listOfCoveragePrints",
    ):
        for item in record.get(key) or []:
            if isinstance(item, dict):
                items.append(item)
    return items


def first_url(items: list[dict[str, Any]]) -> Any:
    """Return the first source URL available across the flattened coverage."""

    for item in items:
        url = item.get("url")
        if url:
            return url
    return None


def parse_month(published_date: Any) -> str:
    """Convert a feed publishedDate into the Inputs/articles/YYYY-MM bucket."""

    if not published_date:
        raise ValueError("record is missing publishedDate")
    return datetime.fromisoformat(str(published_date).replace("Z", "+00:00")).strftime("%Y-%m")


def target_for(record: dict[str, Any]) -> Target:
    """Build the canonical target path components for one feed record."""

    month = parse_month(record.get("publishedDate"))
    article_id = record.get("articleId")
    if article_id is None:
        raise ValueError("record is missing articleId")
    title = record.get("articleTitle") or record.get("contentTitle") or "untitled"
    return Target(month=month, filename=f"{article_id}-{slugify(title)}.md")


def render_note(record: dict[str, Any]) -> str:
    """Convert one raw JSON article object into a Markdown intake note.

    This is a schema mapping only. It intentionally does not add wikilinks,
    infer entities, rewrite the summary into compiled article sections, or touch
    any entity domain files.
    """

    coverage = coverage_items(record)
    outlets = [
        slugify(item.get("displayName", ""))
        for item in coverage
        if item.get("displayName")
    ]
    countries = [
        item.get("country")
        for item in coverage
        if item.get("country")
    ]
    media = record.get("listOfMedia") or []
    if not isinstance(media, list):
        media = []

    title = record.get("articleTitle") or record.get("contentTitle") or ""
    body = str(record.get("contentDescription") or "").strip()
    url = first_url(coverage)

    lines = [
        "---",
        f"articleId: {yaml_quote(record.get('articleId'))}",
        f"articleTitle: {yaml_quote(title)}",
        f"publishedDate: {yaml_quote(record.get('publishedDate'))}",
        f"category: {yaml_quote(record.get('category') or '')}",
        f"topic: {yaml_quote(record.get('topic') or '')}",
        f"tone: {yaml_quote(record.get('tone') or '')}",
        f"toneSentiment: {yaml_quote(record.get('toneSentiment') or '')}",
        f"eventType: {yaml_quote(record.get('eventType') or '')}",
        f"tags: {yaml_flow_list(record.get('listOfTags') or [])}",
        f"outlets: {yaml_flow_list(outlets)}",
        f"countries: {yaml_flow_list(countries)}",
        f"coverageCount: {len(coverage)}",
        f"mediaCount: {len(media)}",
        "sourceType: feed",
        f"url: {yaml_quote(url) if url else 'null'}",
        "---",
        "",
        body,
        "",
    ]
    return "\n".join(lines)


def load_records(path: Path) -> list[dict[str, Any]]:
    """Read a raw feed export and validate the expected top-level shape."""

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a top-level JSON array")
    records: list[dict[str, Any]] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{path} record {index} is not a JSON object")
        records.append(item)
    return records


def raw_file_for_month(month: str) -> Path:
    """Resolve --month YYYY-MM to the Animation Technology feed export naming convention."""

    try:
        dt = datetime.strptime(month, "%Y-%m")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("month must use YYYY-MM format") from exc
    return RAW_FEED_DIR / f"{dt.year}_{dt.strftime('%b').lower()}_feed_data.txt"


def resolve_raw_files(args: argparse.Namespace) -> list[Path]:
    """Resolve --all, --month, and explicit paths into unique absolute files."""

    paths: list[Path] = []
    if args.all:
        paths.extend(sorted(RAW_FEED_DIR.glob("*.txt")))
    if args.month:
        paths.extend(raw_file_for_month(month) for month in args.month)
    if args.raw_files:
        paths.extend(Path(path) for path in args.raw_files)

    seen: set[Path] = set()
    resolved: list[Path] = []
    for path in paths:
        candidate = path if path.is_absolute() else ROOT / path
        candidate = candidate.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        resolved.append(candidate)
    return resolved


def scan_frontmatter_issues(paths: list[Path]) -> list[str]:
    """Run a lightweight post-write scan for known YAML-risk patterns.

    PyYAML is not guaranteed to be installed in every runtime that touches this
    vault, so this check stays dependency-free. It is not a complete YAML parser;
    it only catches the exact classes of mistakes that have previously broken
    this corpus: bare punctuation-heavy scalars and unquoted hash tags.
    """

    issues: list[str] = []
    scalar_keys = {"articleTitle", "category", "topic", "tone", "toneSentiment", "eventType"}
    list_keys = {"tags", "outlets", "countries"}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        end = text.find("\n---", 4)
        if not text.startswith("---\n") or end == -1:
            issues.append(f"{path}: missing or unterminated frontmatter")
            continue
        for line_no, line in enumerate(text[4:end].splitlines(), start=1):
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            value = value.strip()
            if key in scalar_keys and not (value.startswith("'") or value == "null"):
                issues.append(f"{path}:{line_no}: unquoted scalar: {line}")
            if key in list_keys and "#" in value:
                inner = value.split("[", 1)[1].rsplit("]", 1)[0] if "[" in value and "]" in value else value
                for item in inner.split(","):
                    item = item.strip()
                    if "#" in item and not (item.startswith("'") and item.endswith("'")):
                        issues.append(f"{path}:{line_no}: unquoted hash list item: {line}")
    return issues


def process_file(path: Path, write: bool, overwrite: bool) -> tuple[Counter[str], list[Path]]:
    """Dry-run or write all records from one raw feed export.

    Existing matching files are skipped in both Inputs and entities by default.
    That matters because a successful ingest moves files from Inputs into
    entities/article; after that move, re-running this importer should report
    the records as already processed rather than recreate duplicate intake notes.
    """

    records = load_records(path)
    targets = [target_for(record) for record in records]
    duplicate_targets = sum(1 for _, count in Counter((t.month, t.filename) for t in targets).items() if count > 1)

    counts: Counter[str] = Counter()
    counts["records"] = len(records)
    counts["duplicate_targets"] = duplicate_targets
    written_paths: list[Path] = []

    for record, target in zip(records, targets):
        counts[f"month:{target.month}"] += 1
        if target.input_path.exists() and not overwrite:
            counts["existing_inputs"] += 1
            continue
        if target.entity_path.exists() and not overwrite:
            counts["existing_entities"] += 1
            continue
        counts["would_write" if not write else "written"] += 1
        if not write:
            continue
        target.input_path.parent.mkdir(parents=True, exist_ok=True)
        target.input_path.write_text(render_note(record), encoding="utf-8")
        written_paths.append(target.input_path)

    return counts, written_paths


def print_counts(path: Path, counts: Counter[str], elapsed: float) -> None:
    """Print tab-separated run metrics for easy copying into receipts/logs."""

    months = ", ".join(
        f"{key.removeprefix('month:')}:{counts[key]}"
        for key in sorted(k for k in counts if k.startswith("month:"))
    )
    print(f"source_file\t{path.relative_to(ROOT)}")
    print(f"records_seen\t{counts['records']}")
    print(f"target_months\t{months}")
    print(f"would_write_new\t{counts['would_write']}")
    print(f"written\t{counts['written']}")
    print(f"existing_inputs\t{counts['existing_inputs']}")
    print(f"existing_entities\t{counts['existing_entities']}")
    print(f"duplicate_targets\t{counts['duplicate_targets']}")
    print(f"elapsed_seconds\t{elapsed:.3f}")


def main(argv: list[str] | None = None) -> int:
    """Command-line entrypoint."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("raw_files", nargs="*", help="Specific raw feed .txt files to process")
    parser.add_argument("--month", action="append", help="Process one feed month, e.g. 2026-05")
    parser.add_argument("--all", action="store_true", help="Process every .txt file under raw/feed data/")
    parser.add_argument("--write", action="store_true", help="Write files. Without this, perform a dry run.")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing matching Inputs/entities files")
    args = parser.parse_args(argv)

    raw_files = resolve_raw_files(args)
    if not raw_files:
        parser.error("provide --all, --month YYYY-MM, or at least one raw feed file")

    if args.overwrite and not args.write:
        parser.error("--overwrite only makes sense with --write")

    exit_code = 0
    total_written: list[Path] = []
    for raw_file in raw_files:
        if not raw_file.exists():
            print(f"Missing raw file: {raw_file}", file=sys.stderr)
            exit_code = 1
            continue
        start = time.time()
        try:
            counts, written = process_file(raw_file, write=args.write, overwrite=args.overwrite)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            exit_code = 1
            continue
        print_counts(raw_file, counts, time.time() - start)
        total_written.extend(written)

    # Only scan newly written files. Dry runs and fully-skipped reruns should be
    # fast and read-only, while write runs get an immediate safety check.
    if total_written:
        issues = scan_frontmatter_issues(total_written)
        print(f"frontmatter_files_checked\t{len(total_written)}")
        print(f"frontmatter_issues\t{len(issues)}")
        for issue in issues[:20]:
            print(issue)
        if issues:
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
