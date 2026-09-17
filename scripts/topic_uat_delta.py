#!/usr/bin/env python3
"""Prepare and verify an unloaded, topic-only wiki-to-UAT delta."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

from project_wiki_to_uat import _projection_core, scan_wiki
from wiki_uat_projection import (
    ROOT,
    TARGET_DATABASE,
    ProjectionError,
    canonical_json,
    fetch_uat_projections,
    run_mysql,
    sha256_file,
    verify_hashed_manifest,
    write_hashed_manifest,
)


SCHEMA = "wiki-uat-topic-delta.v1"


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def verify_dir(bundle_dir: Path) -> dict[str, Any]:
    manifest = verify_hashed_manifest(bundle_dir)
    if manifest.get("schemaVersion") != SCHEMA:
        raise ProjectionError("unexpected topic-delta schema")
    if manifest.get("targetDatabase") != TARGET_DATABASE:
        raise ProjectionError("topic delta does not target UAT")
    if manifest.get("loadPermitted") is not False:
        raise ProjectionError("topic delta must remain explicitly unloaded")
    rows = read_rows(bundle_dir / "topic-updates.ndjson")
    counts = manifest.get("counts") or {}
    if len(rows) != int(counts.get("topicUpdates", -1)):
        raise ProjectionError("topic update row count differs from manifest")
    ids = [int(row["articleId"]) for row in rows]
    source_ids = [str(row["sourceId"]) for row in rows]
    if len(ids) != len(set(ids)) or len(source_ids) != len(set(source_ids)):
        raise ProjectionError("topic delta contains duplicate identities")
    expected_keys = {"articleId", "sourceId", "articlePath", "fromTopic", "toTopic"}
    for row in rows:
        if set(row) != expected_keys or row["fromTopic"] == row["toTopic"]:
            raise ProjectionError("topic delta row is malformed or unchanged")
    forbidden = (
        int(counts.get("inserts", -1)),
        int(counts.get("deletes", -1)),
        int(counts.get("unauthorizedParentChanges", -1)),
        int(counts.get("childChanges", -1)),
    )
    if forbidden != (0, 0, 0, 0):
        raise ProjectionError(f"topic delta contains forbidden differences: {forbidden}")
    if any(path.name.endswith(".sql") for path in bundle_dir.iterdir() if path.is_file()):
        raise ProjectionError("unloaded topic delta must not contain executable SQL")
    return manifest


def prepare(args: argparse.Namespace) -> int:
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ProjectionError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    notes = scan_wiki(expected_count=None)
    projected: dict[int, tuple[str, dict[str, Any], str]] = {}
    for source_id, note in notes.items():
        projection = note["projection"]
        if not projection:
            raise ProjectionError(f"article lacks Database Projection: {source_id}")
        article_id = int(projection["identity"]["uatArticleId"])
        if article_id in projected:
            raise ProjectionError(f"wiki projections share article ID {article_id}")
        projected[article_id] = (source_id, projection, note["relativePath"])

    live = fetch_uat_projections()
    insert_ids = sorted(set(projected) - set(live))
    delete_ids = sorted(set(live) - set(projected))
    topic_updates = []
    unauthorized = []
    child_changes = []
    child_keys = ("coverage", "media", "tags", "userGroups")
    for article_id in sorted(set(projected) & set(live)):
        source_id, projection, article_path = projected[article_id]
        expected = _projection_core(projection)
        observed = _projection_core(live[article_id])
        expected_article = expected["article"]
        observed_article = observed["article"]
        changed_parent = sorted(
            key for key in set(expected_article) | set(observed_article)
            if expected_article.get(key) != observed_article.get(key)
        )
        if changed_parent == ["topic"]:
            topic_updates.append({
                "articleId": article_id,
                "sourceId": source_id,
                "articlePath": article_path,
                "fromTopic": observed_article.get("topic"),
                "toTopic": expected_article.get("topic"),
            })
        elif changed_parent:
            unauthorized.append({
                "articleId": article_id,
                "sourceId": source_id,
                "fields": changed_parent,
            })
        changed_children = [
            key for key in child_keys
            if canonical_json(expected[key]) != canonical_json(observed[key])
        ]
        if changed_children:
            child_changes.append({
                "articleId": article_id,
                "sourceId": source_id,
                "collections": changed_children,
            })

    counts = {
        "wikiArticles": len(projected),
        "uatArticles": len(live),
        "inserts": len(insert_ids),
        "deletes": len(delete_ids),
        "topicUpdates": len(topic_updates),
        "unchangedParents": len(projected) - len(insert_ids) - len(topic_updates) - len(unauthorized),
        "unauthorizedParentChanges": len(unauthorized),
        "childChanges": len(child_changes),
    }
    proof = {
        "schemaVersion": SCHEMA,
        "counts": counts,
        "insertIdSample": insert_ids[:20],
        "deleteIdSample": delete_ids[:20],
        "unauthorizedParentSample": unauthorized[:20],
        "childChangeSample": child_changes[:20],
    }
    updates_path = output / "topic-updates.ndjson"
    updates_path.write_text(
        "".join(canonical_json(row) + "\n" for row in topic_updates),
        encoding="utf-8",
    )
    proof_path = output / "live-diff-proof.json"
    proof_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "schemaVersion": SCHEMA,
        "targetDatabase": TARGET_DATABASE,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "loadPermitted": False,
        "counts": counts,
    }
    write_hashed_manifest(output, manifest, [updates_path, proof_path])
    verified = verify_dir(output)
    print(json.dumps({"bundle": output.relative_to(ROOT).as_posix(), **verified["counts"], "verified": True, "loaded": False}, indent=2))
    return 0


def verify(args: argparse.Namespace) -> int:
    manifest = verify_dir(args.bundle_dir.resolve())
    print(json.dumps({"bundle": args.bundle_dir.resolve().relative_to(ROOT).as_posix(), **manifest["counts"], "verified": True, "loaded": False}, indent=2))
    return 0


def freeze_wiki(args: argparse.Namespace) -> int:
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ProjectionError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    notes = scan_wiki(expected_count=None)
    rows = []
    article_ids = set()
    for source_id, note in notes.items():
        projection = note["projection"]
        if not projection:
            raise ProjectionError(f"article lacks Database Projection: {source_id}")
        article_id = int(projection["identity"]["uatArticleId"])
        if article_id in article_ids:
            raise ProjectionError(f"wiki projections share article ID {article_id}")
        article_ids.add(article_id)
        rows.append({
            "articleId": article_id,
            "sourceId": source_id,
            "articlePath": note["relativePath"],
            "projection": _projection_core(projection),
        })
    rows.sort(key=lambda row: row["articleId"])
    (output / "wiki-projections.ndjson").write_text(
        "".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8"
    )
    state = {"schemaVersion": SCHEMA, "wikiArticles": len(rows)}
    (output / "prepare-state.json").write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(state, indent=2))
    return 0


def fetch_part(args: argparse.Namespace) -> int:
    output = args.output_dir.resolve()
    state = json.loads((output / "prepare-state.json").read_text(encoding="utf-8"))
    rows = read_rows(output / "wiki-projections.ndjson")
    if len(rows) != int(state["wikiArticles"]):
        raise ProjectionError("frozen wiki projection count differs")
    start = args.start
    end = min(start + args.limit, len(rows))
    if start < 0 or start >= len(rows):
        raise ProjectionError("UAT part start is outside article range")
    selected = rows[start:end]
    live = fetch_uat_projections(row["articleId"] for row in selected)
    topic_updates = []
    inserts = []
    unauthorized = []
    child_changes = []
    for row in selected:
        article_id = int(row["articleId"])
        if article_id not in live:
            inserts.append(article_id)
            continue
        expected = row["projection"]
        observed = _projection_core(live[article_id])
        expected_article = expected["article"]
        observed_article = observed["article"]
        changed_parent = sorted(
            key for key in set(expected_article) | set(observed_article)
            if expected_article.get(key) != observed_article.get(key)
        )
        if changed_parent == ["topic"]:
            topic_updates.append({
                "articleId": article_id,
                "sourceId": row["sourceId"],
                "articlePath": row["articlePath"],
                "fromTopic": observed_article.get("topic"),
                "toTopic": expected_article.get("topic"),
            })
        elif changed_parent:
            unauthorized.append({"articleId": article_id, "sourceId": row["sourceId"], "fields": changed_parent})
        changed = [
            key for key in ("coverage", "media", "tags", "userGroups")
            if canonical_json(expected[key]) != canonical_json(observed[key])
        ]
        if changed:
            child_changes.append({"articleId": article_id, "sourceId": row["sourceId"], "collections": changed})
    part_dir = output / "live-parts"
    part_dir.mkdir(exist_ok=True)
    payload = {
        "schemaVersion": SCHEMA,
        "start": start,
        "end": end,
        "inserts": inserts,
        "topicUpdates": topic_updates,
        "unauthorizedParentChanges": unauthorized,
        "childChanges": child_changes,
    }
    path = part_dir / f"{start:05d}-{end:05d}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"part": path.name, "articles": end-start, "inserts": len(inserts), "topicUpdates": len(topic_updates), "unauthorizedParentChanges": len(unauthorized), "childChanges": len(child_changes)}, indent=2))
    return 0


def finalize(args: argparse.Namespace) -> int:
    output = args.output_dir.resolve()
    state = json.loads((output / "prepare-state.json").read_text(encoding="utf-8"))
    rows = read_rows(output / "wiki-projections.ndjson")
    parts = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((output / "live-parts").glob("*.json"))]
    covered = sorted(index for part in parts for index in range(part["start"], part["end"]))
    if covered != list(range(len(rows))):
        raise ProjectionError("UAT comparison parts are incomplete or overlapping")
    wiki_ids = {int(row["articleId"]) for row in rows}
    live_ids = {int(line) for line in run_mysql("SELECT `article_id` FROM `UAT_articles` ORDER BY `article_id`;\n").splitlines() if line.strip()}
    insert_ids = sorted({value for part in parts for value in part["inserts"]})
    if insert_ids != sorted(wiki_ids - live_ids):
        raise ProjectionError("part insert set differs from live UAT identity set")
    delete_ids = sorted(live_ids - wiki_ids)
    topic_updates = [row for part in parts for row in part["topicUpdates"]]
    unauthorized = [row for part in parts for row in part["unauthorizedParentChanges"]]
    child_changes = [row for part in parts for row in part["childChanges"]]
    counts = {
        "wikiArticles": len(wiki_ids), "uatArticles": len(live_ids),
        "inserts": len(insert_ids), "deletes": len(delete_ids),
        "topicUpdates": len(topic_updates),
        "unchangedParents": len(wiki_ids) - len(insert_ids) - len(topic_updates) - len(unauthorized),
        "unauthorizedParentChanges": len(unauthorized), "childChanges": len(child_changes),
    }
    proof = {
        "schemaVersion": SCHEMA, "counts": counts,
        "insertIdSample": insert_ids[:20], "deleteIdSample": delete_ids[:20],
        "unauthorizedParentSample": unauthorized[:20], "childChangeSample": child_changes[:20],
    }
    updates_path = output / "topic-updates.ndjson"
    updates_path.write_text("".join(canonical_json(row) + "\n" for row in topic_updates), encoding="utf-8")
    proof_path = output / "live-diff-proof.json"
    proof_path.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "schemaVersion": SCHEMA, "targetDatabase": TARGET_DATABASE,
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(), "loadPermitted": False,
        "counts": counts,
    }
    files = [output / "wiki-projections.ndjson", output / "prepare-state.json", updates_path, proof_path, *sorted((output / "live-parts").glob("*.json"))]
    write_hashed_manifest(output, manifest, files)
    verified = verify_dir(output)
    print(json.dumps({"bundle": output.relative_to(ROOT).as_posix(), **verified["counts"], "verified": True, "loaded": False}, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare_parser = sub.add_parser("prepare")
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    prepare_parser.set_defaults(func=prepare)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--bundle-dir", type=Path, required=True)
    verify_parser.set_defaults(func=verify)
    freeze_parser = sub.add_parser("freeze-wiki")
    freeze_parser.add_argument("--output-dir", type=Path, required=True)
    freeze_parser.set_defaults(func=freeze_wiki)
    part_parser = sub.add_parser("fetch-part")
    part_parser.add_argument("--output-dir", type=Path, required=True)
    part_parser.add_argument("--start", type=int, required=True)
    part_parser.add_argument("--limit", type=int, default=1000)
    part_parser.set_defaults(func=fetch_part)
    finalize_parser = sub.add_parser("finalize")
    finalize_parser.add_argument("--output-dir", type=Path, required=True)
    finalize_parser.set_defaults(func=finalize)
    try:
        args = parser.parse_args()
        return int(args.func(args))
    except (ProjectionError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
