#!/usr/bin/env python3
"""Repair the bounded topic regression created by the first loose-goal tranche.

The first 50 articles in the 2026-08-14 loose-article goal were cascaded before
``ingest_cascade.py`` enforced the canonical 80-topic taxonomy. Their raw tags
therefore became 37 temporary Topic Entity notes. This tool repairs only the
articles named by the supplied manifest; it does not compile or re-cascade them.

Safety properties:

* every manifest identity must resolve to exactly one compiled article;
* every active noncanonical Topic Entity must cover only a manifest article;
* classifications use the same frozen taxonomy/classifier as the live cascade;
* all 80 canonical Coverage sections are rebuilt from compiled article links;
* source text, enrichment metadata, non-topic links, and all non-topic database
  projection fields are preserved;
* retired notes are moved into a recoverable archive, never deleted;
* all candidate output is validated before the first source-of-truth write;
* any write-time failure restores every changed note and archive move.

Default mode is read-only. Pass ``--apply`` to commit the validated repair.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from topic_consolidation import (
    ARTICLE_LOG,
    ARTICLE_ROOT,
    AUDIT_HEADING,
    PROJECTION_HEADING,
    ROOT,
    TOPIC_LOG,
    TOPIC_ROOT,
    canonical_json,
    classify,
    extract_projection,
    extract_section,
    load_taxonomy,
    read_frontmatter,
    render_audit,
    render_projection,
    replace_last_updated,
    replace_section,
    rewrite_related_entities,
    source_fragments,
    title_from_body,
    topic_links_from_section,
)


class RepairError(RuntimeError):
    pass


ARTICLE_LINK = re.compile(r"\[\[(article/[^|\]#]+)(?:\|([^\]]+))?\]\]")


def source_ids_from_manifest(path: Path) -> list[str]:
    values = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        value = Path(line).name.split("-", 1)[0]
        if not value:
            raise RepairError(f"manifest line has no source ID: {line}")
        values.append(value)
    if not values or len(values) != len(set(values)):
        raise RepairError("manifest source IDs are empty or duplicated")
    return values


def compiled_by_source_id() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for path in sorted(ARTICLE_ROOT.glob("????-??/*.md")):
        fm, _, _ = read_frontmatter(path.read_text(encoding="utf-8"))
        source_id = str(fm.get("sourceId") or "")
        if source_id in found:
            raise RepairError(f"duplicate compiled sourceId: {source_id}")
        found[source_id] = path
    return found


def render_article(text: str, row: dict[str, Any], display_by_id: dict[str, str], bundle_id: str, timestamp: str) -> str:
    _, raw_fm, body = read_frontmatter(text)
    projection = extract_projection(body)
    selected = [{"topicId": row["primary"]}, *({"topicId": value} for value in row["secondary"])]
    body = rewrite_related_entities(body, selected, display_by_id)
    body = replace_section(body, AUDIT_HEADING, render_audit(row, bundle_id, timestamp), before=PROJECTION_HEADING)
    projection["article"]["topic"] = display_by_id[row["primary"]]
    body = replace_section(body, PROJECTION_HEADING, render_projection(projection))
    raw_fm = replace_last_updated(raw_fm, timestamp)
    return "---\n" + raw_fm + "\n---\n\n" + body.rstrip() + "\n"


def update_topic_note(text: str, coverage: list[tuple[str, str]]) -> str:
    fm, raw_fm, body = read_frontmatter(text)
    fm["articleCount"] = len(coverage)
    raw_fm = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1_000_000).rstrip()
    content = "\n".join(f"- [[{target}|{label}]]" for target, label in coverage)
    body = replace_section(body, "Coverage", content)
    return "---\n" + raw_fm + "\n---\n\n" + body.rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--assignment-report",
        type=Path,
        help="Reuse a previously validated repair report as the frozen assignment set.",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    manifest = args.manifest.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    taxonomy_payload, topics = load_taxonomy()
    canonical_ids = {str(row["topicId"]) for row in topics}
    display_by_id = {str(row["topicId"]): str(row["displayName"]) for row in topics}
    if len(canonical_ids) != 80:
        raise RepairError(f"expected 80 canonical topics, found {len(canonical_ids)}")

    source_ids = source_ids_from_manifest(manifest)
    prior_report = None
    prior_by_source: dict[str, dict[str, Any]] = {}
    if args.assignment_report:
        prior_report = json.loads(args.assignment_report.resolve().read_text(encoding="utf-8"))
        prior_by_source = {str(row["sourceId"]): row for row in prior_report.get("assignments", [])}
        if set(prior_by_source) != set(source_ids):
            raise RepairError("frozen assignment report does not exactly cover the manifest")
    compiled = compiled_by_source_id()
    missing = sorted(set(source_ids) - set(compiled))
    if missing:
        raise RepairError(f"manifest articles missing from compiled corpus: {missing[:10]}")
    target_paths = [compiled[source_id] for source_id in source_ids]
    target_article_links = {
        f"article/{path.parent.name}/{path.stem}" for path in target_paths
    }

    system = {"index.md", "catalog.md", "log.md", ".DS_Store"}
    topic_entity_paths = sorted(
        path for path in TOPIC_ROOT.glob("*.md")
        if path.name not in system and not path.name.startswith("log-")
    )
    extras = [path for path in topic_entity_paths if path.stem not in canonical_ids]
    for path in extras:
        text = path.read_text(encoding="utf-8")
        fm, _, body = read_frontmatter(text)
        if str(fm.get("category")) != "Imported article topic":
            raise RepairError(f"noncanonical topic is not a known imported regression: {path.name}")
        coverage = {match.group(1) for match in ARTICLE_LINK.finditer(extract_section(body, "Coverage"))}
        if not coverage or not coverage <= target_article_links:
            raise RepairError(f"noncanonical topic has coverage outside bounded manifest: {path.name}")

    digest = hashlib.sha256()
    digest.update(manifest.read_bytes())
    digest.update((ROOT / "topics" / "canonical-topics.yaml").read_bytes())
    digest.update("\n".join(path.name for path in extras).encode("utf-8"))
    bundle_id = str(prior_report["bundleId"]) if prior_report else digest.hexdigest()[:16]
    timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")

    original_articles = {path: path.read_text(encoding="utf-8") for path in target_paths}
    new_articles: dict[Path, str] = {}
    assignments = []
    for path in target_paths:
        fragments = source_fragments(path, original_articles[path])
        if prior_report:
            row = prior_by_source[str(fragments["sourceId"])]
        else:
            result = classify(fragments, topics)
            row = {
                "articlePath": path.relative_to(ROOT).as_posix(),
                "sourceId": fragments["sourceId"],
                "originalProjectionTopic": fragments["projectionTopic"],
                "originalTopicLinks": fragments["topicLinks"],
                **result,
            }
        assignments.append(row)
        _, _, current_body = read_frontmatter(original_articles[path])
        current_projection = extract_projection(current_body)
        current_topic_ids = [item["topicId"] for item in topic_links_from_section(current_body)]
        expected_topic_ids = [row["primary"], *row["secondary"]]
        if (
            current_projection["article"].get("topic") == display_by_id[row["primary"]]
            and current_topic_ids == expected_topic_ids
        ):
            new_articles[path] = original_articles[path]
        else:
            new_articles[path] = render_article(original_articles[path], row, display_by_id, bundle_id, timestamp)

    coverage: dict[str, dict[str, str]] = defaultdict(dict)
    for path in sorted(ARTICLE_ROOT.glob("????-??/*.md")):
        text = new_articles.get(path) or path.read_text(encoding="utf-8")
        _, _, body = read_frontmatter(text)
        label = title_from_body(body, path.stem)
        target = f"article/{path.parent.name}/{path.stem}"
        topic_ids = [row["topicId"] for row in topic_links_from_section(body)]
        unknown = sorted(set(topic_ids) - canonical_ids)
        if unknown:
            raise RepairError(f"compiled article retains noncanonical topic links: {path}: {unknown}")
        for topic_id in topic_ids:
            coverage[topic_id][target] = label

    canonical_paths = {topic_id: TOPIC_ROOT / f"{topic_id}.md" for topic_id in canonical_ids}
    missing_notes = sorted(topic_id for topic_id, path in canonical_paths.items() if not path.exists())
    if missing_notes:
        raise RepairError(f"canonical topic notes missing: {missing_notes}")
    original_topics = {path: path.read_text(encoding="utf-8") for path in canonical_paths.values()}
    new_topics = {
        path: update_topic_note(
            original_topics[path],
            sorted(coverage[topic_id].items()),
        )
        for topic_id, path in canonical_paths.items()
    }

    report = {
        "schemaVersion": "goal-topic-regression-repair.v1",
        "bundleId": bundle_id,
        "manifestArticles": len(target_paths),
        "canonicalTopics": len(canonical_ids),
        "noncanonicalTopicsToArchive": len(extras),
        "changedArticles": sum(new_articles[path] != original_articles[path] for path in target_paths),
        "changedCanonicalTopicNotes": sum(new_topics[path] != original_topics[path] for path in new_topics),
        "assignmentSource": "frozen-report" if prior_report else "classifier",
        "assignments": assignments,
        "archivedTopicFiles": [path.relative_to(ROOT).as_posix() for path in extras],
        "applied": bool(args.apply),
    }
    report_path = output / "repair-report.json"

    if not args.apply:
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key not in {"assignments", "archivedTopicFiles"}}, indent=2))
        return 0

    archive_dir = ROOT / "archive" / "topic-legacy" / f"goal-topic-repair-{bundle_id}"
    if archive_dir.exists() and any(archive_dir.iterdir()):
        raise RepairError(f"archive directory is not empty: {archive_dir}")
    archive_dir.mkdir(parents=True, exist_ok=True)
    original_article_log = ARTICLE_LOG.read_text(encoding="utf-8")
    original_topic_log = TOPIC_LOG.read_text(encoding="utf-8")
    moved: list[tuple[Path, Path]] = []
    try:
        for path, text in new_articles.items():
            path.write_text(text, encoding="utf-8")
        for path, text in new_topics.items():
            path.write_text(text, encoding="utf-8")
        for path in extras:
            destination = archive_dir / path.name
            shutil.move(str(path), str(destination))
            moved.append((path, destination))
        with ARTICLE_LOG.open("a", encoding="utf-8") as handle:
            for row in assignments:
                handle.write(
                    f"- {timestamp} | source: goal topic regression repair {bundle_id} | entity: "
                    f"[[{row['articlePath'][:-3]}]] | action: updated — canonical topic set to "
                    f"{display_by_id[row['primary']]} with {len(row['secondary'])} secondary topic(s); "
                    "original topic metadata preserved in the audit section | reasoning: corrected the bounded first-tranche cascade regression.\n"
                )
        with TOPIC_LOG.open("a", encoding="utf-8") as handle:
            handle.write(
                f"- {timestamp} | source: goal topic regression repair {bundle_id} | action: repaired — "
                f"archived {len(extras)} noncanonical imported topic notes and rebuilt exact coverage for "
                f"{len(canonical_ids)} canonical topics from {len(compiled)} compiled articles | reasoning: restored the approved taxonomy cap.\n"
            )
        remaining = [
            path for path in TOPIC_ROOT.glob("*.md")
            if path.name not in system and not path.name.startswith("log-") and path.stem not in canonical_ids
        ]
        if remaining:
            raise RepairError(f"noncanonical active topics remain: {[path.name for path in remaining]}")
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception:
        for path, text in original_articles.items():
            path.write_text(text, encoding="utf-8")
        for path, text in original_topics.items():
            path.write_text(text, encoding="utf-8")
        ARTICLE_LOG.write_text(original_article_log, encoding="utf-8")
        TOPIC_LOG.write_text(original_topic_log, encoding="utf-8")
        for original, destination in reversed(moved):
            if destination.exists():
                shutil.move(str(destination), str(original))
        raise

    print(json.dumps({key: value for key, value in report.items() if key not in {"assignments", "archivedTopicFiles"}}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RepairError, OSError, ValueError, KeyError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
