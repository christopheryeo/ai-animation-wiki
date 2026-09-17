#!/usr/bin/env python3
"""Retire structurally incomplete imported topic notes safely.

The former raw-metadata cascade created ``category: Imported article topic``
notes outside the approved canonical topic list.  This utility retires only
those notes after proving that every linked article already retains at least
one valid canonical topic.  It removes only the obsolete topic links from the
articles' ``## Related Entities`` sections, archives the legacy notes for
provenance, appends an audit entry, and rebuilds the Topic catalog.

Run without ``--apply`` to perform the complete preflight.  ``--apply`` is
rollback-safe for note rewrites and archive moves if a later write fails.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
TOPIC_DIR = ROOT / "entities" / "topic"
ARTICLE_DIR = ROOT / "entities" / "article"
TOPIC_LOG = TOPIC_DIR / "log.md"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}
ARTICLE_LINK = re.compile(r"^\s*-\s*\[\[article/([^|\]]+)(?:\|[^\]]*)?\]\]\s*$")
TOPIC_LINK = re.compile(r"\[\[topic/([^|\]#]+)(?:\|[^\]]*)?\]\]")


class RepairError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyTopic:
    path: Path
    topic_id: str
    display_name: str
    articles: tuple[str, ...]


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise RepairError("note lacks frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise RepairError("note lacks closing frontmatter delimiter")
    data = yaml.safe_load(text[4:end]) or {}
    if not isinstance(data, dict):
        raise RepairError("frontmatter is not a mapping")
    return data, text[end + 5 :]


def replace_section(body: str, heading: str, content: str) -> str:
    pattern = re.compile(rf"(?ms)^## {re.escape(heading)}\s*$\n.*?(?=^## |\Z)")
    matches = list(pattern.finditer(body))
    if len(matches) != 1:
        raise RepairError(f"expected one ## {heading} section, found {len(matches)}")
    return pattern.sub(f"## {heading}\n{content.rstrip()}\n\n", body, count=1).rstrip() + "\n"


def topic_notes() -> list[Path]:
    return sorted(
        path for path in TOPIC_DIR.glob("*.md")
        if path.name not in SYSTEM_FILES and not path.name.startswith("log-")
    )


def read_legacy_topics() -> list[LegacyTopic]:
    result = []
    for path in topic_notes():
        data, body = split_frontmatter(path.read_text(encoding="utf-8"))
        if data.get("category") != "Imported article topic":
            continue
        coverage_match = re.search(r"(?ms)^## Coverage\s*$\n(.*?)(?=^## |\Z)", body)
        if not coverage_match:
            raise RepairError(f"{path.name}: legacy topic lacks ## Coverage")
        coverage = coverage_match.group(1)
        article_targets = []
        for line in coverage.splitlines():
            match = ARTICLE_LINK.match(line)
            if match:
                article_targets.append(match.group(1))
        if not article_targets:
            raise RepairError(f"{path.name}: legacy topic has no article Coverage links")
        if len(article_targets) != len(set(article_targets)):
            raise RepairError(f"{path.name}: duplicate article Coverage links")
        result.append(LegacyTopic(
            path=path,
            topic_id=str(data.get("topicId") or path.stem),
            display_name=str(data.get("displayName") or path.stem),
            articles=tuple(article_targets),
        ))
    return result


def article_path(target: str) -> Path:
    path = ARTICLE_DIR / f"{target}.md"
    if not path.is_file():
        raise RepairError(f"missing compiled article for {target}")
    return path


def active_canonical_ids(legacy_ids: set[str]) -> set[str]:
    ids = set()
    for path in topic_notes():
        if path.stem in legacy_ids:
            continue
        data, _ = split_frontmatter(path.read_text(encoding="utf-8"))
        if data.get("status") != "active":
            continue
        ids.add(str(data.get("topicId") or path.stem))
    if not ids:
        raise RepairError("no active canonical topic notes found")
    return ids


def remove_legacy_links(text: str, legacy_ids: set[str], canonical_ids: set[str]) -> tuple[str, int]:
    _, body = split_frontmatter(text)
    section_match = re.search(r"(?ms)^## Related Entities\s*$\n(.*?)(?=^## |\Z)", body)
    if not section_match:
        raise RepairError("article lacks ## Related Entities")
    lines = section_match.group(1).splitlines()
    remaining = []
    removed = 0
    for line in lines:
        targets = TOPIC_LINK.findall(line)
        legacy_targets = [target for target in targets if target in legacy_ids]
        if not legacy_targets:
            remaining.append(line)
            continue
        if len(targets) != 1 or not re.fullmatch(r"\s*-\s*\[\[topic/[^\]]+\]\]\s*", line):
            raise RepairError(f"legacy topic link is not an isolated Related Entities line: {line!r}")
        removed += 1
    canonical_remaining = {
        target
        for line in remaining
        for target in TOPIC_LINK.findall(line)
        if target in canonical_ids
    }
    if not canonical_remaining:
        raise RepairError("article would have no active canonical topic after legacy-link removal")
    return text.replace(section_match.group(0), "## Related Entities\n" + "\n".join(remaining).rstrip() + "\n\n", 1), removed


def update_last_updated(text: str, timestamp: str) -> str:
    """Record the permitted operational timestamp without reformatting YAML."""
    if not re.search(r"(?m)^last_updated:", text):
        return text
    return re.sub(r"(?m)^last_updated:.*$", f"last_updated: {timestamp}", text, count=1)


def preflight() -> tuple[list[LegacyTopic], dict[Path, tuple[str, int]]]:
    legacy = read_legacy_topics()
    if not legacy:
        return [], {}
    legacy_ids = {row.topic_id for row in legacy}
    if len(legacy_ids) != len(legacy):
        raise RepairError("duplicate legacy topicId values")
    canonical_ids = active_canonical_ids(legacy_ids)
    article_rewrites: dict[Path, tuple[str, int]] = {}
    covered_by = {}
    for topic in legacy:
        for target in topic.articles:
            covered_by.setdefault(target, set()).add(topic.topic_id)
    for target in sorted(covered_by):
        path = article_path(target)
        updated, removed = remove_legacy_links(path.read_text(encoding="utf-8"), legacy_ids, canonical_ids)
        expected = len(covered_by[target])
        if removed != expected:
            raise RepairError(
                f"{target}: expected {expected} legacy links from Coverage, removed {removed}"
            )
        article_rewrites[path] = (updated, removed)
    return legacy, article_rewrites


def apply(legacy: list[LegacyTopic], rewrites: dict[Path, tuple[str, int]], archive_dir: Path) -> dict:
    if archive_dir.exists():
        raise RepairError(f"archive destination already exists: {archive_dir}")
    originals = {path: path.read_text(encoding="utf-8") for path in rewrites}
    timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    moved: list[tuple[Path, Path]] = []
    try:
        for path, (text, _) in rewrites.items():
            path.write_text(update_last_updated(text, timestamp), encoding="utf-8")
        archive_dir.mkdir(parents=True)
        for topic in legacy:
            destination = archive_dir / topic.path.name
            shutil.move(str(topic.path), str(destination))
            moved.append((topic.path, destination))
        with TOPIC_LOG.open("a", encoding="utf-8") as handle:
            for topic in legacy:
                handle.write(
                    f"- {timestamp} | source: legacy imported-topic retirement | entity: "
                    f"[[{topic.topic_id}|{topic.display_name}]] | action: archived — moved to "
                    f"archive/topic-legacy/{archive_dir.name}/{topic.path.name}; all direct article "
                    "links retained an active canonical topic | reasoning: remove the incomplete "
                    "pre-canonical topic record after structural validation.\n"
                )
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_catalog.py"), "topic"], cwd=ROOT, check=True)
    except Exception:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8")
        for source, destination in reversed(moved):
            if destination.exists():
                shutil.move(str(destination), str(source))
        if archive_dir.exists() and not any(archive_dir.iterdir()):
            archive_dir.rmdir()
        raise
    return {
        "archivedTopics": len(legacy),
        "rewrittenArticles": len(rewrites),
        "removedLegacyLinks": sum(count for _, count in rewrites.values()),
        "archive": archive_dir.relative_to(ROOT).as_posix(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the validated retirement")
    parser.add_argument("--archive-name", help="archive directory name under archive/topic-legacy")
    args = parser.parse_args()
    try:
        legacy, rewrites = preflight()
        report = {
            "legacyTopics": len(legacy),
            "affectedArticles": len(rewrites),
            "legacyLinksToRemove": sum(count for _, count in rewrites.values()),
            "dryRun": not args.apply,
        }
        if not legacy:
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0
        if args.apply:
            name = args.archive_name or f"legacy-topic-normalisation-{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}"
            report.update(apply(legacy, rewrites, ROOT / "archive" / "topic-legacy" / name))
            report["dryRun"] = False
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except (RepairError, OSError, subprocess.CalledProcessError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
