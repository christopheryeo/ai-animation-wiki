#!/usr/bin/env python3
"""Export the frozen UAT-only population into verified cascade input notes.

The tool is deliberately split into three commands:

``prepare``
    Reads canonical ``AI_Animation_UAT`` tables with SELECT queries only and
    writes a deterministic, hashed bundle. It never writes into ``Inputs``.

``verify-bundle``
    Recomputes every bundle hash and validates the exact 5,192-record/month
    contract, identities, projections, and note names.

``apply``
    Re-verifies the bundle and copies one reviewed month (or all months) into
    ``Inputs/articles``. It refuses every overwrite and leaves the bundle
    untouched for audit and retry.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from wiki_uat_projection import (
    PROJECTION_SCHEMA,
    ROOT,
    TARGET_DATABASE,
    ProjectionError,
    canonical_json,
    fetch_uat_projections,
    record_hash,
    render_projection,
    run_mysql,
    verify_hashed_manifest,
    write_hashed_manifest,
)


BASELINE_DIR = ROOT / "runs" / "uat-projection"
DEFAULT_RECONCILIATION = BASELINE_DIR / "reconciliation-baseline.json"
DEFAULT_OUTPUT = BASELINE_DIR / "uat-to-inputs-bundle"
INPUT_ROOT = ROOT / "Inputs" / "articles"
EXPECTED_TOTAL = 5_192
EXPECTED_MONTHS = {
    "2026-03": 690,
    "2026-04": 1_629,
    "2026-05": 1_957,
    "2026-06": 916,
}
EXPECTED_UAT_COUNT = 22_004
EXPECTED_UAT_CHECKSUMS = {
    "UAT_articles": 3_284_786_772,
    "UAT_article_coverage": 3_435_154_212,
    "UAT_article_media": 133_383_945,
    "UAT_article_tags": 4_225_005_483,
    "UAT_article_user_groups": 786_520_603,
}


def _iso(value: Any) -> str:
    return "" if value is None else str(value).replace(" ", "T")


def _distinct(values: list[Any]) -> list[str]:
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _input_metadata(projection: dict[str, Any]) -> dict[str, Any]:
    article = projection["article"]
    coverage = projection["coverage"]
    source_url = next(
        (str(row["url"]) for row in coverage if row.get("url")),
        "",
    )
    source_id = projection["identity"]["wikiSourceId"]
    return {
        "articleId": source_id,
        "articleTitle": article.get("article_title") or article.get("content_title") or source_id,
        "contentTitle": article.get("content_title"),
        "publishedDate": _iso(article.get("published_date")),
        "category": article.get("category"),
        "topic": article.get("topic"),
        "tone": article.get("tone"),
        "toneSentiment": article.get("tone_sentiment"),
        "eventType": article.get("event_type"),
        "tags": list(projection["tags"]),
        "outlets": _distinct([row.get("display_name") for row in coverage]),
        "countries": _distinct([row.get("country") for row in coverage]),
        "coverageCount": len(coverage),
        "mediaCount": len(projection["media"]),
        "sourceType": "feed",
        "url": source_url,
        "uatLegacyArticleId": article["article_id"],
    }


def render_input_note(projection: dict[str, Any]) -> str:
    metadata = _input_metadata(projection)
    article = projection["article"]
    source_text = (
        article.get("content_description")
        or article.get("content_title")
        or article.get("article_title")
        or f"Legacy UAT article {article['article_id']}"
    )
    frontmatter = yaml.safe_dump(
        metadata,
        allow_unicode=True,
        sort_keys=False,
        width=1_000_000,
    ).rstrip()
    return (
        f"---\n{frontmatter}\n---\n\n"
        f"{str(source_text).strip()}\n\n"
        f"{render_projection(projection)}"
    )


def _load_targets(path: Path) -> list[dict[str, Any]]:
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("uatOnlyCount") != EXPECTED_TOTAL:
        raise ProjectionError("frozen reconciliation does not contain 5,192 UAT-only records")
    targets = baseline.get("uatOnly")
    if not isinstance(targets, list) or len(targets) != EXPECTED_TOTAL:
        raise ProjectionError("frozen UAT-only identity list is missing or incomplete")
    months = Counter(str(row["month"]) for row in targets)
    if dict(sorted(months.items())) != EXPECTED_MONTHS:
        raise ProjectionError(f"frozen UAT-only month counts differ: {dict(months)}")
    ids = [int(row["targetArticleId"]) for row in targets]
    if len(ids) != len(set(ids)):
        raise ProjectionError("frozen UAT-only list contains duplicate UAT article IDs")
    return sorted(targets, key=lambda row: (row["month"], int(row["targetArticleId"])))


def verify_live_uat_baseline() -> None:
    """Break out if the frozen canonical UAT population has drifted."""

    count_output = run_mysql("SELECT COUNT(*) FROM `UAT_articles`;").strip()
    try:
        count = int(count_output)
    except ValueError as exc:
        raise ProjectionError(f"invalid live UAT count response: {count_output!r}") from exc
    if count != EXPECTED_UAT_COUNT:
        raise ProjectionError(
            f"live UAT count drifted: expected {EXPECTED_UAT_COUNT}, found {count}"
        )
    table_list = ",".join(f"`{name}`" for name in EXPECTED_UAT_CHECKSUMS)
    output = run_mysql(f"CHECKSUM TABLE {table_list};")
    observed = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            raise ProjectionError(f"invalid CHECKSUM TABLE response: {line!r}")
        observed[parts[0].rsplit(".", 1)[-1]] = int(parts[1])
    if observed != EXPECTED_UAT_CHECKSUMS:
        raise ProjectionError(
            f"live UAT canonical checksums drifted: expected "
            f"{EXPECTED_UAT_CHECKSUMS}, found {observed}"
        )


def _validate_projection(
    projection: dict[str, Any],
    target: dict[str, Any],
) -> None:
    article_id = int(target["targetArticleId"])
    expected_source_id = f"uat-legacy-{article_id}"
    identity = projection["identity"]
    if identity != {
        "wikiSourceId": expected_source_id,
        "uatArticleId": article_id,
        "origin": "uat-legacy",
    }:
        raise ProjectionError(f"invalid identity for UAT article {article_id}")
    if int(projection["article"]["article_id"]) != article_id:
        raise ProjectionError(f"parent identity mismatch for UAT article {article_id}")
    if not (
        projection["article"].get("article_title")
        or projection["article"].get("content_title")
    ):
        raise ProjectionError(f"UAT article {article_id} has no meaningful title")
    if not (
        projection["article"].get("content_description")
        or projection["article"].get("content_title")
        or projection["article"].get("article_title")
    ):
        raise ProjectionError(f"UAT article {article_id} has no meaningful source text")


def prepare(args: argparse.Namespace) -> int:
    started = time.monotonic()
    targets = _load_targets(args.reconciliation.resolve())
    verify_live_uat_baseline()
    identity_by_id = {
        int(row["targetArticleId"]): {
            "wikiSourceId": f"uat-legacy-{int(row['targetArticleId'])}",
            "origin": "uat-legacy",
        }
        for row in targets
    }
    projections = fetch_uat_projections(identity_by_id.keys(), identity_by_id)
    if len(projections) != EXPECTED_TOTAL:
        raise ProjectionError(
            f"live UAT returned {len(projections)} of {EXPECTED_TOTAL} required records"
        )

    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ProjectionError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    records_path = output / "records.ndjson"
    identity_path = output / "identity-map.tsv"
    record_lines = []
    identity_lines = ["wiki_source_id\tuat_article_id\torigin\tmonth\tprojection_sha256"]
    note_paths: list[Path] = []

    target_by_id = {int(row["targetArticleId"]): row for row in targets}
    for article_id in sorted(projections):
        target = target_by_id[article_id]
        projection = projections[article_id]
        _validate_projection(projection, target)
        month = str(target["month"])
        note_dir = output / "inputs" / month
        note_dir.mkdir(parents=True, exist_ok=True)
        note_path = note_dir / f"uat-legacy-{article_id}.md"
        note_path.write_text(render_input_note(projection), encoding="utf-8")
        note_paths.append(note_path)
        projection_sha = record_hash(projection)
        record_lines.append(canonical_json({
            "month": month,
            "projection": projection,
            "projectionSha256": projection_sha,
            "relativeInputPath": note_path.relative_to(output).as_posix(),
        }))
        identity_lines.append(
            f"uat-legacy-{article_id}\t{article_id}\tuat-legacy\t{month}\t{projection_sha}"
        )

    records_path.write_text("\n".join(record_lines) + "\n", encoding="utf-8")
    identity_path.write_text("\n".join(identity_lines) + "\n", encoding="utf-8")
    elapsed = time.monotonic() - started
    manifest = {
        "bundleVersion": "uat-to-inputs-bundle.v1",
        "targetDatabase": TARGET_DATABASE,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceReconciliation": args.reconciliation.resolve().relative_to(ROOT).as_posix(),
        "counts": {
            "articles": EXPECTED_TOTAL,
            "months": EXPECTED_MONTHS,
            "blankSourceUrls": sum(
                not _input_metadata(value)["url"] for value in projections.values()
            ),
            "identityCollisionsResolvedByPrefix": 205,
        },
        "timing": {
            "elapsedSeconds": round(elapsed, 6),
            "averageSecondsPerArticle": round(elapsed / EXPECTED_TOTAL, 9),
        },
    }
    write_hashed_manifest(
        output,
        manifest,
        [records_path, identity_path, *note_paths],
    )
    verify_bundle_dir(output)
    print(f"Prepared {EXPECTED_TOTAL} UAT-only articles in {elapsed:.2f} seconds")
    print(f"Average: {elapsed / EXPECTED_TOTAL:.6f} seconds/article")
    print(f"Bundle: {output.relative_to(ROOT)}")
    return 0


def verify_bundle_dir(bundle_dir: Path) -> dict[str, Any]:
    manifest = verify_hashed_manifest(bundle_dir)
    if manifest.get("bundleVersion") != "uat-to-inputs-bundle.v1":
        raise ProjectionError("unexpected UAT-to-input bundle version")
    if manifest.get("targetDatabase") != TARGET_DATABASE:
        raise ProjectionError("bundle target is not AI_Animation_UAT")
    counts = manifest.get("counts", {})
    if counts.get("articles") != EXPECTED_TOTAL or counts.get("months") != EXPECTED_MONTHS:
        raise ProjectionError("bundle counts differ from the approved contract")

    records_path = bundle_dir / "records.ndjson"
    records = [
        json.loads(line)
        for line in records_path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    if len(records) != EXPECTED_TOTAL:
        raise ProjectionError("records.ndjson does not contain exactly 5,192 rows")
    months: Counter[str] = Counter()
    source_ids: set[str] = set()
    article_ids: set[int] = set()
    for record in records:
        projection = record["projection"]
        article_id = int(projection["identity"]["uatArticleId"])
        target = {"targetArticleId": article_id}
        _validate_projection(projection, target)
        if record["projectionSha256"] != record_hash(projection):
            raise ProjectionError(f"projection hash mismatch for {article_id}")
        source_id = projection["identity"]["wikiSourceId"]
        if source_id in source_ids or article_id in article_ids:
            raise ProjectionError(f"duplicate identity in bundle: {source_id}")
        source_ids.add(source_id)
        article_ids.add(article_id)
        month = str(record["month"])
        months[month] += 1
        expected_path = f"inputs/{month}/uat-legacy-{article_id}.md"
        if record["relativeInputPath"] != expected_path:
            raise ProjectionError(f"unexpected input path for {article_id}")
        note_path = bundle_dir / expected_path
        note = note_path.read_text(encoding="utf-8")
        if canonical_json(projection) not in note:
            raise ProjectionError(f"input note projection mismatch for {article_id}")
    if dict(sorted(months.items())) != EXPECTED_MONTHS:
        raise ProjectionError(f"bundle month counts differ: {dict(months)}")
    return manifest


def verify(args: argparse.Namespace) -> int:
    started = time.monotonic()
    manifest = verify_bundle_dir(args.bundle_dir.resolve())
    elapsed = time.monotonic() - started
    print(f"Verified {manifest['counts']['articles']} articles in {elapsed:.2f} seconds")
    print(f"Average: {elapsed / EXPECTED_TOTAL:.6f} seconds/article")
    return 0


def apply(args: argparse.Namespace) -> int:
    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    verify_bundle_dir(bundle_dir)
    months = [args.month] if args.month else sorted(EXPECTED_MONTHS)
    for month in months:
        if month not in EXPECTED_MONTHS:
            raise ProjectionError(f"month is outside approved import set: {month}")
    sources = [
        path
        for month in months
        for path in sorted((bundle_dir / "inputs" / month).glob("*.md"))
    ]
    expected = sum(EXPECTED_MONTHS[month] for month in months)
    if len(sources) != expected:
        raise ProjectionError(f"selected bundle contains {len(sources)} of {expected} notes")
    collisions = [
        INPUT_ROOT / source.parent.name / source.name
        for source in sources
        if (INPUT_ROOT / source.parent.name / source.name).exists()
    ]
    if collisions:
        raise ProjectionError(
            f"refusing to overwrite {len(collisions)} existing input notes; "
            f"first={collisions[0]}"
        )
    for source in sources:
        destination = INPUT_ROOT / source.parent.name / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    elapsed = time.monotonic() - started
    print(f"Materialised {len(sources)} verified inputs in {elapsed:.2f} seconds")
    print(f"Average: {elapsed / len(sources):.6f} seconds/article")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--reconciliation", type=Path, default=DEFAULT_RECONCILIATION)
    prepare_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    prepare_parser.set_defaults(func=prepare)
    verify_parser = subparsers.add_parser("verify-bundle")
    verify_parser.add_argument("--bundle-dir", type=Path, required=True)
    verify_parser.set_defaults(func=verify)
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--bundle-dir", type=Path, required=True)
    apply_parser.add_argument("--month", choices=sorted(EXPECTED_MONTHS))
    apply_parser.set_defaults(func=apply)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return int(args.func(args))
    except (ProjectionError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
