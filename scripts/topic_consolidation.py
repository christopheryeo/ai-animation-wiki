#!/usr/bin/env python3
"""Prepare, apply, and verify the AI-controlled topic consolidation.

The workflow is intentionally bundle-driven:

  freeze          hash the live source-of-truth notes and make recovery archives
  prepare         classify every legacy topic and article into the canonical taxonomy
  verify-bundle   verify hashes, completeness, limits, and assignment invariants
  apply           apply a verified bundle in restartable batches and rebuild topics
  verify-applied  prove preservation, reciprocal links/counts, and idempotence

No command reads or writes MySQL. UAT delta preparation remains a separate approval-gated
projection step after this tool's post-apply verification passes.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
ARTICLE_ROOT = ROOT / "entities" / "article"
TOPIC_ROOT = ROOT / "entities" / "topic"
ARTICLE_LOG = ARTICLE_ROOT / "log.md"
TOPIC_LOG = TOPIC_ROOT / "log.md"
TAXONOMY_PATH = ROOT / "topics" / "canonical-topics.yaml"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", ".DS_Store"}
AUDIT_HEADING = "Topic Consolidation Audit"
PROJECTION_HEADING = "Database Projection"
BUNDLE_VERSION = "topic-consolidation-bundle.v1"
BASELINE_VERSION = "topic-consolidation-baseline.v1"


class ConsolidationError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def topic_paths() -> list[Path]:
    return sorted(
        p for p in TOPIC_ROOT.glob("*.md")
        if p.name not in SYSTEM_FILES and not p.name.startswith("log-")
    )


def article_paths() -> list[Path]:
    return sorted(ARTICLE_ROOT.glob("????-??/*.md"))


def rel(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def read_frontmatter(text: str) -> tuple[dict[str, Any], str, str]:
    if not text.startswith("---\n"):
        raise ConsolidationError("note has no YAML frontmatter")
    end = text.find("\n---", 4)
    if end < 0:
        raise ConsolidationError("note has unclosed YAML frontmatter")
    raw = text[4:end]
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ConsolidationError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise ConsolidationError("frontmatter is not a mapping")
    return data, raw, text[end + 4 :].lstrip("\n")


def extract_section(body: str, heading: str) -> str:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n?(.*?)(?=^## |\Z)",
        body,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group(1).rstrip() if match else ""


def replace_section(body: str, heading: str, content: str, *, before: str | None = None) -> str:
    rendered = f"## {heading}\n{content.rstrip()}\n"
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$\n?.*?(?=^## |\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    if pattern.search(body):
        # Use a callable replacement so backslashes preserved in JSON/YAML content are
        # treated as literal text rather than regular-expression replacement escapes.
        return pattern.sub(lambda _: rendered + "\n", body, count=1).rstrip() + "\n"
    if before:
        marker = re.search(rf"^## {re.escape(before)}\s*$", body, flags=re.MULTILINE)
        if marker:
            return (body[: marker.start()].rstrip() + "\n\n" + rendered + "\n" + body[marker.start():]).rstrip() + "\n"
    return body.rstrip() + "\n\n" + rendered


def remove_section(body: str, heading: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$\n?.*?(?=^## |\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    return pattern.sub("", body, count=1).strip() + "\n"


def extract_projection(body: str) -> dict[str, Any]:
    section = extract_section(body, PROJECTION_HEADING)
    match = re.search(r"```json\s*\n(.*?)\n```", section, flags=re.DOTALL)
    if not match:
        raise ConsolidationError("missing canonical Database Projection JSON")
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ConsolidationError(f"invalid Database Projection JSON: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("article"), dict):
        raise ConsolidationError("Database Projection has unexpected shape")
    return value


def render_projection(value: dict[str, Any]) -> str:
    return "```json\n" + canonical_json(value) + "\n```"


def wikilink(target: str, display: str) -> str:
    return f"[[{target}|{display}]]"


def topic_links_from_section(body: str) -> list[dict[str, str]]:
    section = extract_section(body, "Related Entities")
    found: list[dict[str, str]] = []
    for match in re.finditer(r"\[\[topic/([^|\]#]+)(?:\|([^\]]+))?\]\]", section):
        found.append({"topicId": match.group(1), "display": match.group(2) or match.group(1)})
    seen = set()
    return [row for row in found if not (row["topicId"] in seen or seen.add(row["topicId"]))]


def issue_tags(body: str) -> list[str]:
    return [
        line[2:].strip()
        for line in extract_section(body, "Issue Tags").splitlines()
        if line.startswith("- ") and line[2:].strip() and "None recorded" not in line
    ]


def title_from_body(body: str, fallback: str) -> str:
    match = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    return match.group(1).strip() if match else fallback


# Absolute ceiling on the configurable registry maximum. Raised from 80 to 90 to implement
# entities/decisions/expand-canonical-topic-cap-to-90.md (accepted 2026-08-19), which raised
# maximumActiveTopics in topics/canonical-topics.yaml but was never propagated to this guard.
ABSOLUTE_MAXIMUM_ACTIVE_TOPICS = 90


def load_taxonomy(path: Path = TAXONOMY_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("topics"), list):
        raise ConsolidationError("canonical taxonomy has unexpected shape")
    topics = payload["topics"]
    ids = [str(row.get("topicId") or "") for row in topics]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ConsolidationError("canonical taxonomy has blank or duplicate topic IDs")
    maximum = int(payload.get("maximumActiveTopics") or 80)
    starter_empty = payload.get("starter") is True and len(topics) == 0
    if not starter_empty and not 60 <= len(topics) <= maximum <= ABSOLUTE_MAXIMUM_ACTIVE_TOPICS:
        raise ConsolidationError(
            f"canonical taxonomy count {len(topics)} is outside 60–{maximum} or maximum exceeds "
            f"{ABSOLUTE_MAXIMUM_ACTIVE_TOPICS}"
        )
    for row in topics:
        for field in ("topicId", "displayName", "category", "definition", "keywords"):
            if not row.get(field):
                raise ConsolidationError(f"canonical topic {row.get('topicId')} lacks {field}")
        try:
            row["_patterns"] = [re.compile(value, re.I) for value in row["keywords"]]
            row["_combined_pattern"] = re.compile(
                "|".join(f"(?:{value})" for value in row["keywords"]), re.I
            )
        except re.error as exc:
            raise ConsolidationError(f"invalid keyword regex for {row['topicId']}: {exc}") from exc
    return payload, topics


def write_ndjson(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def read_ndjson(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def tar_paths(output: Path, paths: list[Path]) -> None:
    with tarfile.open(output, "w:gz") as archive:
        for path in paths:
            archive.add(path, arcname=rel(path), recursive=False)


def manifest_entry(path: Path) -> dict[str, Any]:
    return {"path": rel(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def verify_file_manifest(
    rows: list[dict[str, Any]],
    *,
    ignore_paths: set[str] | None = None,
) -> None:
    failures = []
    for row in rows:
        if row["path"] in (ignore_paths or set()):
            continue
        path = ROOT / row["path"]
        if not path.is_file():
            failures.append(f"missing {row['path']}")
        elif path.stat().st_size != row["bytes"] or sha256_file(path) != row["sha256"]:
            failures.append(f"changed {row['path']}")
        if len(failures) >= 20:
            break
    if failures:
        raise ConsolidationError("frozen source differs: " + "; ".join(failures))


def add_bundle_hashes(bundle_dir: Path, manifest: dict[str, Any], files: list[Path]) -> None:
    manifest["bundleFiles"] = {
        path.relative_to(bundle_dir).as_posix(): {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in sorted(files)
    }
    (bundle_dir / "bundle_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_hashed_bundle(
    bundle_dir: Path,
    expected_version: str,
    *,
    allowed_extras: set[str] | None = None,
) -> dict[str, Any]:
    path = bundle_dir / "bundle_manifest.json"
    if not path.is_file():
        raise ConsolidationError(f"missing bundle manifest: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schemaVersion") != expected_version:
        raise ConsolidationError("unexpected bundle schemaVersion")
    expected = manifest.get("bundleFiles")
    if not isinstance(expected, dict):
        raise ConsolidationError("bundle manifest lacks bundleFiles")
    actual = {
        p.relative_to(bundle_dir).as_posix()
        for p in bundle_dir.rglob("*")
        if p.is_file() and p != path
    }
    actual -= allowed_extras or set()
    if actual != set(expected):
        raise ConsolidationError(
            f"bundle file set differs; missing={sorted(set(expected)-actual)}, extra={sorted(actual-set(expected))}"
        )
    for relative, recorded in expected.items():
        item = bundle_dir / relative
        if item.stat().st_size != recorded["bytes"] or sha256_file(item) != recorded["sha256"]:
            raise ConsolidationError(f"bundle hash differs: {relative}")
    return manifest


def freeze(args: argparse.Namespace) -> int:
    started = time.monotonic()
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ConsolidationError(f"baseline output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    topics = topic_paths()
    articles = article_paths()
    topic_rows = [manifest_entry(path) for path in topics]
    article_rows = [manifest_entry(path) for path in articles]
    write_ndjson(output / "topic_files.ndjson", topic_rows)
    write_ndjson(output / "article_files.ndjson", article_rows)
    tar_paths(output / "topics.tar.gz", topics)
    tar_paths(output / "articles.tar.gz", articles)
    verify_file_manifest(topic_rows)
    verify_file_manifest(article_rows)
    # Opening and reading every member proves the recovery archives are not truncated.
    for name, count in (("topics.tar.gz", len(topics)), ("articles.tar.gz", len(articles))):
        with tarfile.open(output / name, "r:gz") as archive:
            members = [m for m in archive.getmembers() if m.isfile()]
            if len(members) != count:
                raise ConsolidationError(f"{name} contains {len(members)} of {count} files")
            for member in members:
                handle = archive.extractfile(member)
                if handle is None:
                    raise ConsolidationError(f"cannot read archive member {member.name}")
                while handle.read(1024 * 1024):
                    pass
    files = [
        output / "topic_files.ndjson",
        output / "article_files.ndjson",
        output / "topics.tar.gz",
        output / "articles.tar.gz",
    ]
    manifest = {
        "schemaVersion": BASELINE_VERSION,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "counts": {"activeTopics": len(topics), "compiledArticles": len(articles)},
        "elapsedSeconds": round(time.monotonic() - started, 6),
    }
    add_bundle_hashes(output, manifest, files)
    verify_hashed_bundle(output, BASELINE_VERSION)
    print(json.dumps({"baseline": rel(output), **manifest["counts"], "verified": True}, indent=2))
    return 0


def source_fragments(path: Path, text: str) -> dict[str, Any]:
    frontmatter, _, body = read_frontmatter(text)
    projection = extract_projection(body)
    original_links = topic_links_from_section(body)
    return {
        "sourceId": str(frontmatter.get("sourceId") or path.stem.split("-", 1)[0]),
        "title": title_from_body(body, path.stem),
        "projectionTopic": projection["article"].get("topic"),
        "issueTags": issue_tags(body),
        "topicLinks": original_links,
        "summary": extract_section(body, "Summary"),
        "keyPoints": extract_section(body, "Key Points"),
        "category": str(projection["article"].get("category") or ""),
    }


def pattern_score(topic: dict[str, Any], sources: list[tuple[str, float]]) -> tuple[float, list[str]]:
    score = 0.0
    evidence: list[str] = []
    for value, weight in sources:
        if not value:
            continue
        # One combined search per source is materially faster over a large article corpus.
        # than evaluating every keyword regex independently. Cap repeated hits so syndicated
        # boilerplate cannot dominate the classification.
        hits = sum(1 for _ in topic["_combined_pattern"].finditer(value))
        if hits:
            score += weight * min(hits, 3)
            evidence.append(value[:180])
    return score, evidence[:5]


def classify(fragments: dict[str, Any], topics: list[dict[str, Any]]) -> dict[str, Any]:
    linked = " ; ".join(row["display"] + " " + row["topicId"].replace("-", " ") for row in fragments["topicLinks"])
    tags = " ; ".join(fragments["issueTags"])
    primary_sources = [
        (str(fragments.get("projectionTopic") or ""), 7.0),
        (tags, 5.0),
        (fragments["title"], 4.0),
        (linked, 3.0),
        (fragments["category"], 2.0),
        (fragments["summary"], 1.5),
        (fragments["keyPoints"], 1.0),
    ]
    review_sources = [
        (fragments["title"], 7.0),
        (str(fragments.get("projectionTopic") or ""), 6.0),
        (tags, 5.0),
        (fragments["summary"], 2.0),
        (linked, 1.0),
    ]
    scores: list[tuple[float, str, list[str]]] = []
    review_scores: list[tuple[float, str]] = []
    for topic in topics:
        score, evidence = pattern_score(topic, primary_sources)
        review_score, _ = pattern_score(topic, review_sources)
        if score > 0:
            scores.append((score, topic["topicId"], evidence))
        if review_score > 0:
            review_scores.append((review_score, topic["topicId"]))
    scores.sort(key=lambda row: (-row[0], row[1]))
    review_scores.sort(key=lambda row: (-row[0], row[1]))
    fallback = not scores
    if fallback:
        # This wiki does not use a catch-all topic.  An article with no canonical
        # AI-animation evidence must stay unassigned for relevance review rather
        # than being misfiled under a legacy, foreign-domain fallback.
        selected = []
        evidence_by_id = {}
    else:
        # Keep secondary topics meaningful relative to the primary rather than accepting any incidental hit.
        floor = max(3.0, scores[0][0] * 0.28)
        selected = [scores[0][1], *[row[1] for row in scores[1:] if row[0] >= floor]][:3]
        evidence_by_id = {row[1]: row[2] for row in scores if row[1] in selected}
    primary_candidate = selected[0] if selected else ""
    review_candidate = review_scores[0][1] if review_scores else primary_candidate
    ambiguous = bool(scores) and (len(scores) == 1 or scores[0][0] - scores[1][0] < 2.0)
    review_resolution = "agreed"
    if ambiguous and review_candidate != primary_candidate:
        # Independent review is advisory; combined evidence wins deterministically and the contradiction is recorded.
        review_resolution = "resolved-by-combined-evidence"
    return {
        "primary": primary_candidate,
        "secondary": selected[1:3],
        "fallback": fallback,
        "ambiguous": ambiguous,
        "reviewCandidate": review_candidate,
        "reviewResolution": review_resolution,
        "scores": [{"topicId": row[1], "score": round(row[0], 3)} for row in scores[:8]],
        "evidence": evidence_by_id,
    }


def legacy_fragments(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fm, _, body = read_frontmatter(text)
    display = str(fm.get("displayName") or path.stem.replace("-", " "))
    aliases = fm.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    return {
        "sourceId": path.stem,
        "title": display,
        "projectionTopic": display,
        "issueTags": [str(value) for value in aliases],
        "topicLinks": [],
        "summary": extract_section(body, "Definition"),
        "keyPoints": extract_section(body, "Notes"),
        "category": str(fm.get("category") or ""),
    }


def prepare(args: argparse.Namespace) -> int:
    started = time.monotonic()
    baseline_dir = args.baseline_dir.resolve()
    baseline = verify_hashed_bundle(baseline_dir, BASELINE_VERSION)
    topic_manifest = read_ndjson(baseline_dir / "topic_files.ndjson")
    article_manifest = read_ndjson(baseline_dir / "article_files.ndjson")
    verify_file_manifest(topic_manifest)
    verify_file_manifest(article_manifest)
    taxonomy_payload, topics = load_taxonomy(args.taxonomy.resolve())
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ConsolidationError(f"migration bundle output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    legacy_rows = []
    for row in topic_manifest:
        path = ROOT / row["path"]
        result = classify(legacy_fragments(path), topics)
        legacy_rows.append({
            "legacyTopicId": path.stem,
            "canonicalPrimary": result["primary"],
            "canonicalSecondary": result["secondary"],
            "fallback": result["fallback"],
            "review": {"ambiguous": result["ambiguous"], "candidate": result["reviewCandidate"], "resolution": result["reviewResolution"]},
            "scores": result["scores"],
        })

    assignment_rows = []
    for row in article_manifest:
        path = ROOT / row["path"]
        text = path.read_text(encoding="utf-8", errors="replace")
        fragments = source_fragments(path, text)
        result = classify(fragments, topics)
        assignment_rows.append({
            "articlePath": row["path"],
            "sourceId": fragments["sourceId"],
            "originalProjectionTopic": fragments["projectionTopic"],
            "originalTopicLinks": fragments["topicLinks"],
            "primary": result["primary"],
            "secondary": result["secondary"],
            "fallback": result["fallback"],
            "review": {"ambiguous": result["ambiguous"], "candidate": result["reviewCandidate"], "resolution": result["reviewResolution"]},
            "scores": result["scores"],
            "evidence": result["evidence"],
        })

    taxonomy_out = output / "canonical-topics.yaml"
    clean_taxonomy = copy.deepcopy(taxonomy_payload)
    for row in clean_taxonomy["topics"]:
        row.pop("_patterns", None)
        row.pop("_combined_pattern", None)
    taxonomy_out.write_text(yaml.safe_dump(clean_taxonomy, allow_unicode=True, sort_keys=False, width=1_000_000), encoding="utf-8")
    write_ndjson(output / "legacy-topic-map.ndjson", legacy_rows)
    write_ndjson(output / "article-topic-assignments.ndjson", assignment_rows)
    review = {
        "articles": len(assignment_rows),
        "legacyTopics": len(legacy_rows),
        "canonicalTopics": len(topics),
        "articleFallbacks": sum(row["fallback"] for row in assignment_rows),
        "articleFallbackRate": round(sum(row["fallback"] for row in assignment_rows) / len(assignment_rows), 8),
        "articleAmbiguous": sum(row["review"]["ambiguous"] for row in assignment_rows),
        "legacyFallbacks": sum(row["fallback"] for row in legacy_rows),
        "primaryCounts": dict(sorted(Counter(row["primary"] for row in assignment_rows).items())),
    }
    (output / "ai-review-summary.json").write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    files = [taxonomy_out, output / "legacy-topic-map.ndjson", output / "article-topic-assignments.ndjson", output / "ai-review-summary.json"]
    manifest = {
        "schemaVersion": BUNDLE_VERSION,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "baseline": rel(baseline_dir),
        "baselineManifestSha256": sha256_file(baseline_dir / "bundle_manifest.json"),
        "counts": {
            "startingActiveTopics": baseline["counts"]["activeTopics"],
            "articles": len(assignment_rows),
            "canonicalTopics": len(topics),
            "articleFallbacks": review["articleFallbacks"],
            "articleAmbiguous": review["articleAmbiguous"],
        },
        "elapsedSeconds": round(time.monotonic() - started, 6),
    }
    add_bundle_hashes(output, manifest, files)
    verify_bundle_dir(output)
    print(json.dumps({"bundle": rel(output), **manifest["counts"], "verified": True}, indent=2))
    return 0


def clean_taxonomy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clean = copy.deepcopy(payload)
    for row in clean["topics"]:
        row.pop("_patterns", None)
        row.pop("_combined_pattern", None)
    return clean


def prepare_legacy(args: argparse.Namespace) -> int:
    """Initialize a restartable bundle and classify the smaller legacy-topic population."""
    started = time.monotonic()
    baseline_dir = args.baseline_dir.resolve()
    verify_hashed_bundle(baseline_dir, BASELINE_VERSION)
    topic_manifest = read_ndjson(baseline_dir / "topic_files.ndjson")
    verify_file_manifest(topic_manifest)
    taxonomy_payload, topics = load_taxonomy(args.taxonomy.resolve())
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ConsolidationError(f"restartable bundle output is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    (output / "article-parts").mkdir()
    (output / "canonical-topics.yaml").write_text(
        yaml.safe_dump(clean_taxonomy_payload(taxonomy_payload), allow_unicode=True, sort_keys=False, width=1_000_000),
        encoding="utf-8",
    )
    legacy_rows = []
    for row in topic_manifest:
        path = ROOT / row["path"]
        result = classify(legacy_fragments(path), topics)
        legacy_rows.append({
            "legacyTopicId": path.stem,
            "canonicalPrimary": result["primary"],
            "canonicalSecondary": result["secondary"],
            "fallback": result["fallback"],
            "review": {"ambiguous": result["ambiguous"], "candidate": result["reviewCandidate"], "resolution": result["reviewResolution"]},
            "scores": result["scores"],
        })
    write_ndjson(output / "legacy-topic-map.ndjson", legacy_rows)
    (output / "restartable_state.json").write_text(
        json.dumps({
            "schemaVersion": "topic-consolidation-prepare-state.v1",
            "baseline": rel(baseline_dir),
            "baselineManifestSha256": sha256_file(baseline_dir / "bundle_manifest.json"),
            "legacyTopics": len(legacy_rows),
            "articleParts": [],
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"legacyTopics": len(legacy_rows), "elapsedSeconds": round(time.monotonic()-started, 3)}, indent=2))
    return 0


def prepare_article_part(args: argparse.Namespace) -> int:
    """Classify one bounded article range so long runs remain resumable."""
    started = time.monotonic()
    output = args.output_dir.resolve()
    state_path = output / "restartable_state.json"
    if not state_path.is_file():
        raise ConsolidationError("run prepare-legacy before prepare-article-part")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    baseline_dir = ROOT / state["baseline"]
    verify_hashed_bundle(baseline_dir, BASELINE_VERSION)
    if sha256_file(baseline_dir / "bundle_manifest.json") != state["baselineManifestSha256"]:
        raise ConsolidationError("restartable state points at a different baseline")
    article_manifest = read_ndjson(baseline_dir / "article_files.ndjson")
    start = args.start
    end = min(len(article_manifest), start + args.limit)
    if start < 0 or start >= len(article_manifest) or end <= start:
        raise ConsolidationError(f"invalid article range {start}:{end}")
    selected_manifest = article_manifest[start:end]
    verify_file_manifest(selected_manifest)
    _, topics = load_taxonomy(output / "canonical-topics.yaml")
    part_path = output / "article-parts" / f"{start:05d}-{end:05d}.ndjson"
    if part_path.exists():
        existing = read_ndjson(part_path)
        if [row["articlePath"] for row in existing] != [row["path"] for row in selected_manifest]:
            raise ConsolidationError(f"existing article part differs: {part_path}")
        print(json.dumps({"part": part_path.name, "articles": len(existing), "noop": True}, indent=2))
        return 0
    rows = []
    for item in selected_manifest:
        path = ROOT / item["path"]
        fragments = source_fragments(path, path.read_text(encoding="utf-8", errors="replace"))
        result = classify(fragments, topics)
        rows.append({
            "articlePath": item["path"],
            "sourceId": fragments["sourceId"],
            "originalProjectionTopic": fragments["projectionTopic"],
            "originalTopicLinks": fragments["topicLinks"],
            "primary": result["primary"],
            "secondary": result["secondary"],
            "fallback": result["fallback"],
            "review": {"ambiguous": result["ambiguous"], "candidate": result["reviewCandidate"], "resolution": result["reviewResolution"]},
            "scores": result["scores"],
            "evidence": result["evidence"],
        })
    write_ndjson(part_path, rows)
    state["articleParts"] = sorted(set(state.get("articleParts", [])) | {part_path.name})
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"part": part_path.name, "articles": len(rows), "elapsedSeconds": round(time.monotonic()-started, 3)}, indent=2))
    return 0


def finalize_restartable(args: argparse.Namespace) -> int:
    """Combine all restartable article parts and seal the hashed migration bundle."""
    started = time.monotonic()
    output = args.output_dir.resolve()
    state_path = output / "restartable_state.json"
    if not state_path.is_file():
        raise ConsolidationError("restartable preparation state is missing")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    baseline_dir = ROOT / state["baseline"]
    baseline = verify_hashed_bundle(baseline_dir, BASELINE_VERSION)
    article_manifest = read_ndjson(baseline_dir / "article_files.ndjson")
    expected_paths = [row["path"] for row in article_manifest]
    assignment_rows = []
    part_files = sorted((output / "article-parts").glob("*.ndjson"))
    for part in part_files:
        assignment_rows.extend(read_ndjson(part))
    by_path = {row["articlePath"]: row for row in assignment_rows}
    if len(by_path) != len(assignment_rows):
        raise ConsolidationError("restartable article parts overlap or duplicate articles")
    missing = [path for path in expected_paths if path not in by_path]
    extra = sorted(set(by_path) - set(expected_paths))
    if missing or extra:
        raise ConsolidationError(f"restartable parts incomplete; missing={len(missing)}, extra={len(extra)}, first={missing[:5] or extra[:5]}")
    assignment_rows = [by_path[path] for path in expected_paths]
    write_ndjson(output / "article-topic-assignments.ndjson", assignment_rows)
    legacy_rows = read_ndjson(output / "legacy-topic-map.ndjson")
    review = {
        "articles": len(assignment_rows),
        "legacyTopics": len(legacy_rows),
        "canonicalTopics": len(load_taxonomy(output / "canonical-topics.yaml")[1]),
        "articleFallbacks": sum(row["fallback"] for row in assignment_rows),
        "articleFallbackRate": round(sum(row["fallback"] for row in assignment_rows) / len(assignment_rows), 8),
        "articleAmbiguous": sum(row["review"]["ambiguous"] for row in assignment_rows),
        "legacyFallbacks": sum(row["fallback"] for row in legacy_rows),
        "primaryCounts": dict(sorted(Counter(row["primary"] for row in assignment_rows).items())),
    }
    (output / "ai-review-summary.json").write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The mutable restartable state is no longer part of the sealed bundle.
    state_path.unlink()
    files = [
        output / "canonical-topics.yaml",
        output / "legacy-topic-map.ndjson",
        output / "article-topic-assignments.ndjson",
        output / "ai-review-summary.json",
        *part_files,
    ]
    manifest = {
        "schemaVersion": BUNDLE_VERSION,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "baseline": rel(baseline_dir),
        "baselineManifestSha256": sha256_file(baseline_dir / "bundle_manifest.json"),
        "counts": {
            "startingActiveTopics": baseline["counts"]["activeTopics"],
            "articles": len(assignment_rows),
            "canonicalTopics": review["canonicalTopics"],
            "articleFallbacks": review["articleFallbacks"],
            "articleAmbiguous": review["articleAmbiguous"],
        },
        "elapsedSeconds": round(time.monotonic() - started, 6),
    }
    add_bundle_hashes(output, manifest, files)
    verify_bundle_dir(output)
    print(json.dumps({"bundle": rel(output), **manifest["counts"], "verified": True}, indent=2))
    return 0


def verify_bundle_dir(
    bundle_dir: Path,
    *,
    allowed_extras: set[str] | None = None,
) -> dict[str, Any]:
    manifest = verify_hashed_bundle(
        bundle_dir,
        BUNDLE_VERSION,
        allowed_extras=allowed_extras,
    )
    baseline_dir = ROOT / manifest["baseline"]
    baseline = verify_hashed_bundle(baseline_dir, BASELINE_VERSION)
    if sha256_file(baseline_dir / "bundle_manifest.json") != manifest["baselineManifestSha256"]:
        raise ConsolidationError("migration bundle points at a different baseline manifest")
    taxonomy_payload, topics = load_taxonomy(bundle_dir / "canonical-topics.yaml")
    topic_ids = {row["topicId"] for row in topics}
    legacy = read_ndjson(bundle_dir / "legacy-topic-map.ndjson")
    assignments = read_ndjson(bundle_dir / "article-topic-assignments.ndjson")
    expected_topics = int(baseline["counts"]["activeTopics"])
    expected_articles = int(baseline["counts"]["compiledArticles"])
    if len(legacy) != expected_topics or len({row["legacyTopicId"] for row in legacy}) != expected_topics:
        raise ConsolidationError("legacy-topic mapping is incomplete or duplicated")
    if len(assignments) != expected_articles or len({row["articlePath"] for row in assignments}) != expected_articles:
        raise ConsolidationError("article assignment mapping is incomplete or duplicated")
    source_ids = [row["sourceId"] for row in assignments]
    if len(source_ids) != len(set(source_ids)):
        raise ConsolidationError("article assignment contains duplicate source IDs")
    invalid = []
    for row in assignments:
        selected = [row["primary"], *row["secondary"]]
        if not row["primary"] or len(selected) > int(taxonomy_payload["assignmentLimit"]):
            invalid.append(row["articlePath"])
        if len(selected) != len(set(selected)) or any(value not in topic_ids for value in selected):
            invalid.append(row["articlePath"])
    if invalid:
        raise ConsolidationError(f"invalid article assignments; first={invalid[:10]}")
    fallback_rate = sum(bool(row["fallback"]) for row in assignments) / len(assignments)
    if fallback_rate > 0.01:
        raise ConsolidationError(f"article fallback rate {fallback_rate:.2%} exceeds 1%")
    unresolved = [row["articlePath"] for row in assignments if row["review"]["resolution"] not in {"agreed", "resolved-by-combined-evidence"}]
    if unresolved:
        raise ConsolidationError(f"independent reviews remain unresolved; first={unresolved[:10]}")
    return manifest


def verify_bundle(args: argparse.Namespace) -> int:
    manifest = verify_bundle_dir(args.bundle_dir.resolve())
    print(json.dumps({"bundle": rel(args.bundle_dir.resolve()), **manifest["counts"], "verified": True}, indent=2))
    return 0


def replace_last_updated(raw_frontmatter: str, timestamp: str) -> str:
    if re.search(r"^last_updated:", raw_frontmatter, flags=re.MULTILINE):
        return re.sub(r"^last_updated:.*$", f"last_updated: {timestamp}", raw_frontmatter, count=1, flags=re.MULTILINE)
    return raw_frontmatter.rstrip() + f"\nlast_updated: {timestamp}"


def rewrite_related_entities(body: str, assignments: list[dict[str, str]], display_by_id: dict[str, str]) -> str:
    section = extract_section(body, "Related Entities")
    retained = [line for line in section.splitlines() if not re.search(r"\[\[topic/", line)]
    while retained and not retained[-1].strip():
        retained.pop()
    topic_lines = [f"- {wikilink('topic/' + row['topicId'], display_by_id[row['topicId']])}" for row in assignments]
    content = "\n".join([*retained, *topic_lines]).strip()
    return replace_section(body, "Related Entities", content or "- None identified")


def render_audit(row: dict[str, Any], bundle_id: str, timestamp: str) -> str:
    value = {
        "schemaVersion": "topic-consolidation-audit.v1",
        "bundleId": bundle_id,
        "migratedAt": timestamp,
        "originalProjectionTopic": row["originalProjectionTopic"],
        "originalTopicLinks": row["originalTopicLinks"],
        "canonicalPrimary": row["primary"],
        "canonicalSecondary": row["secondary"],
    }
    return "```json\n" + canonical_json(value) + "\n```"


def apply_article(path: Path, row: dict[str, Any], display_by_id: dict[str, str], bundle_id: str, timestamp: str) -> bool:
    text = path.read_text(encoding="utf-8")
    fm, raw_fm, body = read_frontmatter(text)
    projection = extract_projection(body)
    selected = [{"topicId": row["primary"]}, *({"topicId": value} for value in row["secondary"])]
    expected_links = [item["topicId"] for item in selected]
    current_links = [item["topicId"] for item in topic_links_from_section(body)]
    current_audit = extract_section(body, AUDIT_HEADING)
    if (
        projection["article"].get("topic") == display_by_id[row["primary"]]
        and current_links == expected_links
        and f'"bundleId":"{bundle_id}"' in current_audit
    ):
        return False
    body = rewrite_related_entities(body, selected, display_by_id)
    body = replace_section(body, AUDIT_HEADING, render_audit(row, bundle_id, timestamp), before=PROJECTION_HEADING)
    projection["article"]["topic"] = display_by_id[row["primary"]]
    body = replace_section(body, PROJECTION_HEADING, render_projection(projection))
    raw_fm = replace_last_updated(raw_fm, timestamp)
    path.write_text("---\n" + raw_fm + "\n---\n\n" + body.rstrip() + "\n", encoding="utf-8")
    return True


def article_label(path: Path) -> str:
    _, _, body = read_frontmatter(path.read_text(encoding="utf-8"))
    return title_from_body(body, path.stem)


def render_topic_note(
    topic: dict[str, Any],
    coverage: list[dict[str, str]],
    timestamp: str,
    existing_aliases: list[str],
    last_crawled_at: Any = None,
) -> str:
    aliases = []
    for value in [topic["displayName"], *existing_aliases]:
        value = str(value).strip()
        if value and value.casefold() != topic["displayName"].casefold() and value not in aliases:
            aliases.append(value)
    fm = {
        "topicId": topic["topicId"],
        "displayName": topic["displayName"],
        "category": topic["category"],
        "aliases": aliases,
        "articleCount": len(coverage),
        "lastCrawledAt": last_crawled_at,
    }
    frontmatter = yaml.safe_dump(fm, allow_unicode=True, sort_keys=False, width=1_000_000).rstrip()
    lines = [
        "---", frontmatter, "---", "", f"# {topic['displayName']}", "", "## Definition", topic["definition"], "", "## Coverage",
        *[f"- {wikilink(row['articleTarget'], row['label'])}" for row in coverage],
        "", "## Notes", f"Canonical AI-controlled topic established by the {timestamp[:10]} consolidation.", "",
    ]
    return "\n".join(lines)


def append_lines(path: Path, lines: list[str]) -> None:
    if not lines:
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line.rstrip() + "\n" for line in lines))


def apply(args: argparse.Namespace) -> int:
    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    state_path = bundle_dir / "applied_state.json"
    if state_path.exists():
        verify_applied_dir(bundle_dir, write_report=False)
        print(json.dumps({"bundle": rel(bundle_dir), "changedArticles": 0, "idempotent": True}, indent=2))
        return 0
    manifest = verify_bundle_dir(bundle_dir, allowed_extras={"apply_progress.json"})
    baseline_dir = ROOT / manifest["baseline"]
    taxonomy_payload, topics = load_taxonomy(bundle_dir / "canonical-topics.yaml")
    display_by_id = {row["topicId"]: row["displayName"] for row in topics}
    assignments = read_ndjson(bundle_dir / "article-topic-assignments.ndjson")
    progress_path = bundle_dir / "apply_progress.json"
    progress = {"completed": []}
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    completed = set(progress.get("completed", []))
    completed_paths: set[str] = set()
    for batch_start in range(0, len(assignments), args.batch_size):
        batch = assignments[batch_start : batch_start + args.batch_size]
        batch_key = f"{batch_start + 1}-{batch_start + len(batch)}"
        if batch_key in completed:
            completed_paths.update(row["articlePath"] for row in batch)
    verify_file_manifest(read_ndjson(baseline_dir / "topic_files.ndjson"))
    verify_file_manifest(
        read_ndjson(baseline_dir / "article_files.ndjson"),
        ignore_paths=completed_paths,
    )
    bundle_id = sha256_file(bundle_dir / "bundle_manifest.json")[:16]
    timestamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    changed_articles = 0
    log_lines = []
    for batch_start in range(0, len(assignments), args.batch_size):
        batch = assignments[batch_start : batch_start + args.batch_size]
        batch_key = f"{batch_start + 1}-{batch_start + len(batch)}"
        if batch_key in completed:
            continue
        for row in batch:
            path = ROOT / row["articlePath"]
            if apply_article(path, row, display_by_id, bundle_id, timestamp):
                changed_articles += 1
                log_lines.append(
                    f"- {timestamp} | source: topic consolidation {bundle_id} | entity: "
                    f"[[article/{path.parent.name}/{path.stem}|{article_label(path)}]] | action: updated — "
                    f"canonical topic set to {display_by_id[row['primary']]} with {len(row['secondary'])} secondary topic(s) "
                    "while preserving original topic metadata in the audit section | reasoning: "
                    "accepted AI topic consolidation decision."
                )
        append_lines(ARTICLE_LOG, log_lines)
        log_lines = []
        completed.add(batch_key)
        progress_path.write_text(json.dumps({"completed": sorted(completed), "updatedAt": timestamp}, indent=2) + "\n", encoding="utf-8")

    archive_dir = ROOT / "archive" / "topic-legacy" / bundle_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    current_topics = topic_paths()
    canonical_ids = set(display_by_id)
    archived = 0
    topic_log_lines = []
    for path in current_topics:
        if path.stem in canonical_ids:
            continue
        destination = archive_dir / path.name
        if destination.exists():
            if sha256_file(destination) != sha256_file(path):
                raise ConsolidationError(f"archive collision differs: {destination}")
            path.unlink()
        else:
            shutil.move(str(path), str(destination))
        archived += 1
        topic_log_lines.append(
            f"- {timestamp} | source: topic consolidation {bundle_id} | entity: [[{path.stem}]] | action: archived — "
            f"moved from active Topic Entity to archive/topic-legacy/{bundle_id}/{path.name} | reasoning: mapped to the canonical taxonomy."
        )

    coverage_by_topic: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in assignments:
        path = ROOT / row["articlePath"]
        target = f"article/{path.parent.name}/{path.stem}"
        label = article_label(path)
        for topic_id in [row["primary"], *row["secondary"]]:
            coverage_by_topic[topic_id].append({"articleTarget": target, "label": label})

    for topic in topics:
        path = TOPIC_ROOT / f"{topic['topicId']}.md"
        existed = path.exists()
        existing_aliases: list[str] = []
        last_crawled_at = None
        if existed:
            fm, _, _ = read_frontmatter(path.read_text(encoding="utf-8"))
            value = fm.get("aliases") or []
            existing_aliases = value if isinstance(value, list) else [str(value)]
            last_crawled_at = fm.get("lastCrawledAt")
        coverage = sorted(coverage_by_topic[topic["topicId"]], key=lambda row: (row["articleTarget"], row["label"]))
        path.write_text(
            render_topic_note(topic, coverage, timestamp, existing_aliases, last_crawled_at),
            encoding="utf-8",
        )
        topic_log_lines.append(
            f"- {timestamp} | source: topic consolidation {bundle_id} | entity: [[{topic['topicId']}|{topic['displayName']}]] | "
            f"action: {'updated' if existed else 'created'} — canonical definition and {len(coverage)} exact article backlinks | "
            "reasoning: accepted AI topic consolidation decision."
        )
    append_lines(TOPIC_LOG, topic_log_lines)

    for domain in ("topic", "article"):
        subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_catalog.py"), domain], cwd=ROOT, check=True)
    state = {
        "schemaVersion": "topic-consolidation-applied.v1",
        "bundleId": bundle_id,
        "appliedAt": timestamp,
        "changedArticles": len(assignments),
        "changedArticlesThisInvocation": changed_articles,
        "archivedTopics": archived,
        "activeTopics": len(topic_paths()),
        "elapsedSeconds": round(time.monotonic() - started, 6),
    }
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # State/progress are intentionally post-manifest operational files; add their hashes to a separate receipt.
    (bundle_dir / "apply_receipt.json").write_text(
        json.dumps({**state, "stateSha256": sha256_file(state_path), "progressSha256": sha256_file(progress_path)}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(state, indent=2))
    return 0


def tar_member_bytes(tar_path: Path, member_name: str) -> bytes:
    with tarfile.open(tar_path, "r:gz") as archive:
        member = archive.getmember(member_name)
        handle = archive.extractfile(member)
        if handle is None:
            raise ConsolidationError(f"cannot read baseline member {member_name}")
        return handle.read()


def compare_article_preservation(before: str, after: str, row: dict[str, Any], display_by_id: dict[str, str]) -> list[str]:
    failures = []
    before_fm, before_raw, before_body = read_frontmatter(before)
    after_fm, after_raw, after_body = read_frontmatter(after)
    before_fm.pop("last_updated", None)
    after_fm.pop("last_updated", None)
    if before_fm != after_fm:
        failures.append("frontmatter changed outside last_updated")
    before_projection = extract_projection(before_body)
    after_projection = extract_projection(after_body)
    expected_projection = copy.deepcopy(before_projection)
    expected_projection["article"]["topic"] = display_by_id[row["primary"]]
    if after_projection != expected_projection:
        failures.append("projection changed outside article.topic or topic differs")
    before_base = remove_section(remove_section(before_body, "Related Entities"), PROJECTION_HEADING)
    after_base = remove_section(remove_section(remove_section(after_body, "Related Entities"), AUDIT_HEADING), PROJECTION_HEADING)
    if before_base != after_base:
        failures.append("article body changed outside authorized sections")
    audit = extract_section(after_body, AUDIT_HEADING)
    match = re.search(r"```json\s*\n(.*?)\n```", audit, flags=re.DOTALL)
    if not match:
        failures.append("missing consolidation audit JSON")
    else:
        value = json.loads(match.group(1))
        if value.get("originalProjectionTopic") != row["originalProjectionTopic"]:
            failures.append("audit does not preserve original projection topic")
        if value.get("originalTopicLinks") != row["originalTopicLinks"]:
            failures.append("audit does not preserve original topic links")
        if value.get("canonicalPrimary") != row["primary"] or value.get("canonicalSecondary") != row["secondary"]:
            failures.append("audit canonical assignment differs")
    current_links = [item["topicId"] for item in topic_links_from_section(after_body)]
    if current_links != [row["primary"], *row["secondary"]]:
        failures.append("article topic links differ from assignment")
    return failures


def catalog_note_count(path: Path) -> int | None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^note_count:\s*(\d+)\s*$", text, flags=re.MULTILINE)
    return int(match.group(1)) if match else None


def verify_applied_dir(bundle_dir: Path, *, write_report: bool = True) -> dict[str, Any]:
    started = time.monotonic()
    verification_extras = {
        p.relative_to(bundle_dir).as_posix()
        for p in (bundle_dir / "verification-parts").glob("*.json")
    } if (bundle_dir / "verification-parts").is_dir() else set()
    manifest = verify_hashed_bundle(
        bundle_dir,
        BUNDLE_VERSION,
        allowed_extras={
            "apply_progress.json",
            "applied_state.json",
            "apply_receipt.json",
            "consolidation-report.json",
            "consolidation-report.md",
        } | verification_extras,
    )
    baseline_dir = ROOT / manifest["baseline"]
    baseline = verify_hashed_bundle(baseline_dir, BASELINE_VERSION)
    assignments = read_ndjson(bundle_dir / "article-topic-assignments.ndjson")
    taxonomy_payload, topics = load_taxonomy(bundle_dir / "canonical-topics.yaml")
    display_by_id = {row["topicId"]: row["displayName"] for row in topics}
    assignment_by_path = {row["articlePath"]: row for row in assignments}
    failures: list[str] = []
    preservation_checked = 0
    part_paths = sorted((bundle_dir / "verification-parts").glob("*.json")) if (bundle_dir / "verification-parts").is_dir() else []
    parts = [json.loads(path.read_text(encoding="utf-8")) for path in part_paths]
    covered = sorted(index for part in parts for index in range(part["start"], part["end"]))
    if covered == list(range(len(assignments))):
        preservation_checked = sum(int(part["checked"]) for part in parts)
        failures.extend(value for part in parts for value in part["failures"])
    else:
        raise ConsolidationError("article preservation verification parts are incomplete or overlapping")

    active = topic_paths()
    active_ids = {p.stem for p in active}
    expected_ids = set(display_by_id)
    if active_ids != expected_ids:
        failures.append(f"active topic IDs differ; missing={sorted(expected_ids-active_ids)}, extra={sorted(active_ids-expected_ids)}")
    registry_maximum = int(load_taxonomy()[0].get("maximumActiveTopics") or 80)
    if not 60 <= len(active) <= registry_maximum:
        failures.append(f"active topic count {len(active)} outside 60–{registry_maximum}")
    catalog_count = catalog_note_count(TOPIC_ROOT / "catalog.md")
    if catalog_count != len(active):
        failures.append(f"topic catalog count {catalog_count} differs from direct count {len(active)}")

    expected_coverage: dict[str, set[str]] = defaultdict(set)
    for row in assignments:
        article_path = ROOT / row["articlePath"]
        target = f"article/{article_path.parent.name}/{article_path.stem}"
        for topic_id in [row["primary"], *row["secondary"]]:
            expected_coverage[topic_id].add(target)
    for topic in topics:
        path = TOPIC_ROOT / f"{topic['topicId']}.md"
        fm, _, body = read_frontmatter(path.read_text(encoding="utf-8"))
        coverage = extract_section(body, "Coverage")
        actual = re.findall(r"\[\[([^|\]#]+)", coverage)
        if len(actual) != len(set(actual)):
            failures.append(f"{rel(path)} has duplicate Coverage backlinks")
        if set(actual) != expected_coverage[topic["topicId"]]:
            failures.append(f"{rel(path)} Coverage set differs from assignments")
        if int(fm.get("articleCount") or -1) != len(expected_coverage[topic["topicId"]]):
            failures.append(f"{rel(path)} articleCount differs from assignments")

    state = json.loads((bundle_dir / "applied_state.json").read_text(encoding="utf-8"))
    archive_dir = ROOT / "archive" / "topic-legacy" / state["bundleId"]
    archived_paths = list(archive_dir.glob("*.md")) if archive_dir.is_dir() else []
    baseline_topics = read_ndjson(baseline_dir / "topic_files.ndjson")
    baseline_ids = {Path(row["path"]).stem for row in baseline_topics}
    archived_ids = {p.stem for p in archived_paths}
    if not baseline_ids <= active_ids | archived_ids:
        failures.append(f"starting topics unaccounted for: {sorted(baseline_ids-(active_ids|archived_ids))[:20]}")

    retired = baseline_ids - active_ids
    explicit_retired = []
    for path in [*article_paths(), *[p for p in ROOT.glob("entities/*/*.md") if p.parent.name not in {"topic", "article"}]]:
        text = path.read_text(encoding="utf-8", errors="replace")
        for target in re.findall(r"\[\[topic/([^|\]#]+)", text):
            if target in retired:
                explicit_retired.append(f"{rel(path)} -> {target}")
                if len(explicit_retired) >= 20:
                    break
    if explicit_retired:
        failures.append("explicit retired topic links remain: " + "; ".join(explicit_retired))

    if len(failures) >= 100:
        failures = failures[:100]
    report = {
        "schemaVersion": "topic-consolidation-report.v1",
        "bundle": rel(bundle_dir),
        "counts": {
            "startingActiveTopics": baseline["counts"]["activeTopics"],
            "endingActiveTopics": len(active),
            "archivedTopicFiles": len(archived_paths),
            "topicReduction": int(baseline["counts"]["activeTopics"]) - len(active),
            "topicReductionPercent": round((int(baseline["counts"]["activeTopics"]) - len(active)) / int(baseline["counts"]["activeTopics"]) * 100, 4),
            "startingArticles": baseline["counts"]["compiledArticles"],
            "endingArticles": len(article_paths()),
            "articlesPreservationChecked": preservation_checked,
            "primaryAssignments": len(assignments),
            "secondaryAssignments": sum(len(row["secondary"]) for row in assignments),
            "fallbackAssignments": sum(bool(row["fallback"]) for row in assignments),
        },
        "gates": {
            "activeTopicRange": 60 <= len(active) <= registry_maximum,
            "catalogMatches": catalog_count == len(active),
            "articlePopulationPreserved": len(article_paths()) == int(baseline["counts"]["compiledArticles"]),
            "articleContentPreserved": not any("article" in value.lower() for value in failures),
            "topicCoverageExact": not any("Coverage" in value or "articleCount" in value for value in failures),
            "startingTopicsAccounted": baseline_ids <= active_ids | archived_ids,
            "noExplicitRetiredTopicLinks": not explicit_retired,
        },
        "failures": failures,
        "elapsedSeconds": round(time.monotonic() - started, 6),
        "averageSecondsPerArticle": round((time.monotonic() - started) / len(assignments), 9),
    }
    if write_report:
        (bundle_dir / "consolidation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        lines = [
            "# Topic Consolidation Report", "",
            f"- Starting active topics: **{report['counts']['startingActiveTopics']:,}**",
            f"- Ending active topics: **{report['counts']['endingActiveTopics']:,}**",
            f"- Archived legacy topic files: **{report['counts']['archivedTopicFiles']:,}**",
            f"- Reduction: **{report['counts']['topicReduction']:,} ({report['counts']['topicReductionPercent']:.2f}%)**",
            f"- Articles preserved: **{report['counts']['endingArticles']:,} of {report['counts']['startingArticles']:,}**", "",
            "## Gates", "",
            *[f"- {'PASS' if value else 'FAIL'} — {key}" for key, value in report["gates"].items()], "",
            "## Failures", "",
            *(report["failures"] or ["None"]), "",
        ]
        (bundle_dir / "consolidation-report.md").write_text("\n".join(lines), encoding="utf-8")
    if failures:
        raise ConsolidationError(f"post-apply verification failed ({len(failures)} findings); first={failures[:10]}")
    return report


def verify_applied(args: argparse.Namespace) -> int:
    report = verify_applied_dir(args.bundle_dir.resolve(), write_report=True)
    print(json.dumps(report, indent=2))
    return 0


def verify_article_part(args: argparse.Namespace) -> int:
    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    verification_extras = {
        p.relative_to(bundle_dir).as_posix()
        for p in (bundle_dir / "verification-parts").glob("*.json")
    } if (bundle_dir / "verification-parts").is_dir() else set()
    manifest = verify_hashed_bundle(
        bundle_dir,
        BUNDLE_VERSION,
        allowed_extras={
            "apply_progress.json", "applied_state.json", "apply_receipt.json",
            "consolidation-report.json", "consolidation-report.md",
        } | verification_extras,
    )
    baseline_dir = ROOT / manifest["baseline"]
    _, topics = load_taxonomy(bundle_dir / "canonical-topics.yaml")
    display_by_id = {row["topicId"]: row["displayName"] for row in topics}
    assignments = read_ndjson(bundle_dir / "article-topic-assignments.ndjson")
    start = args.start
    end = min(start + args.limit, len(assignments))
    if start < 0 or start >= len(assignments):
        raise ConsolidationError("verification part start is outside article range")
    failures: list[str] = []
    checked = 0
    with tarfile.open(baseline_dir / "articles.tar.gz", "r:gz") as archive:
        members = {member.name: member for member in archive.getmembers() if member.isfile()}
        for row in assignments[start:end]:
            path = ROOT / row["articlePath"]
            member = members.get(row["articlePath"])
            if not path.is_file():
                failures.append(f"missing article {row['articlePath']}")
                continue
            if not member:
                failures.append(f"article absent from recovery archive {row['articlePath']}")
                continue
            handle = archive.extractfile(member)
            if handle is None:
                failures.append(f"cannot read recovery member {row['articlePath']}")
                continue
            before = handle.read().decode("utf-8")
            after = path.read_text(encoding="utf-8")
            failures.extend(
                f"{row['articlePath']}: {value}"
                for value in compare_article_preservation(before, after, row, display_by_id)
            )
            checked += 1
    output_dir = bundle_dir / "verification-parts"
    output_dir.mkdir(exist_ok=True)
    payload = {
        "schemaVersion": "topic-consolidation-verification-part.v1",
        "start": start,
        "end": end,
        "checked": checked,
        "failures": failures,
        "elapsedSeconds": round(time.monotonic() - started, 6),
    }
    output = output_dir / f"{start:05d}-{end:05d}.json"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"part": output.name, **payload}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--output-dir", type=Path, required=True)
    freeze_parser.set_defaults(func=freeze)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--baseline-dir", type=Path, required=True)
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    prepare_parser.add_argument("--taxonomy", type=Path, default=TAXONOMY_PATH)
    prepare_parser.set_defaults(func=prepare)
    legacy_parser = sub.add_parser("prepare-legacy")
    legacy_parser.add_argument("--baseline-dir", type=Path, required=True)
    legacy_parser.add_argument("--output-dir", type=Path, required=True)
    legacy_parser.add_argument("--taxonomy", type=Path, default=TAXONOMY_PATH)
    legacy_parser.set_defaults(func=prepare_legacy)
    part_parser = sub.add_parser("prepare-article-part")
    part_parser.add_argument("--output-dir", type=Path, required=True)
    part_parser.add_argument("--start", type=int, required=True)
    part_parser.add_argument("--limit", type=int, default=1000)
    part_parser.set_defaults(func=prepare_article_part)
    finalize_parser = sub.add_parser("finalize-bundle")
    finalize_parser.add_argument("--output-dir", type=Path, required=True)
    finalize_parser.set_defaults(func=finalize_restartable)
    verify_parser = sub.add_parser("verify-bundle")
    verify_parser.add_argument("--bundle-dir", type=Path, required=True)
    verify_parser.set_defaults(func=verify_bundle)
    apply_parser = sub.add_parser("apply")
    apply_parser.add_argument("--bundle-dir", type=Path, required=True)
    apply_parser.add_argument("--batch-size", type=int, default=1000)
    apply_parser.set_defaults(func=apply)
    applied_parser = sub.add_parser("verify-applied")
    applied_parser.add_argument("--bundle-dir", type=Path, required=True)
    applied_parser.set_defaults(func=verify_applied)
    verify_part_parser = sub.add_parser("verify-article-part")
    verify_part_parser.add_argument("--bundle-dir", type=Path, required=True)
    verify_part_parser.add_argument("--start", type=int, required=True)
    verify_part_parser.add_argument("--limit", type=int, default=1000)
    verify_part_parser.set_defaults(func=verify_article_part)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        if getattr(args, "batch_size", 1) < 1 or getattr(args, "batch_size", 1) > 1000:
            raise ConsolidationError("batch size must be between 1 and 1000")
        return int(args.func(args))
    except (ConsolidationError, OSError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
