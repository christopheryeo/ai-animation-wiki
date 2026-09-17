#!/usr/bin/env python3
"""Project the complete compiled Markdown article union into UAT.

Commands are intentionally separated by trust boundary:

``prepare``
    Reads the wiki and a configured UAT target, assembles canonical projections,
    assigns stable IDs to wiki-only records, and writes a hashed bundle. No wiki
    or database write occurs.

``verify-bundle``
    Recomputes bundle hashes and validates identities, parent schemas, child
    multisets, the configured initial delta contract, and rollback artifacts.

``apply-projections``
    Re-verifies the bundle, preflights every note hash, then appends canonical
    projection metadata to notes that do not already have it. This writes only
    Markdown; the compressed originals permit exact rollback.

``diff``
    Compares every projected parent and child multiset with live UAT.

``load``
    Requires an attributed approval file bound to the bundle hash, re-runs the
    configured diff, and executes the rollback-on-failure UAT transaction. The
    target is fixed to ``AI_Animation_UAT``; production is prohibited.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import re
import shutil
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from tag_registry import load_registry as load_tag_registry

from article_quality import split_note
from ingest_cascade import (
    DATABASE_PROJECTION_PATTERN,
    parse_frontmatter,
    split_database_projection,
    title_from_slug,
)
from uat_to_inputs import (
    EXPECTED_UAT_COUNT,
    verify_live_uat_baseline,
)
from wiki_uat_projection import (
    ARTICLE_COLUMNS,
    PROJECTION_SCHEMA,
    ROOT,
    TARGET_DATABASE,
    ProjectionError,
    canonical_json,
    canonical_multiset,
    fetch_uat_projections,
    record_hash,
    render_projection,
    run_mysql,
    run_production_readonly,
    sha256_file,
    verify_hashed_manifest,
    write_hashed_manifest,
)


ARTIFACT_ROOT = ROOT / "runs" / "uat-projection"
RECONCILIATION = ARTIFACT_ROOT / "reconciliation-baseline.json"
IDENTITY_BASELINE = ARTIFACT_ROOT / "uat-identity-baseline.tsv"
DIRECT_REVIEW = ARTIFACT_ROOT / "direct-review.json"
JULY29_REVIEW = ARTIFACT_ROOT / "reviewed-enrichment.json"
JULY23_ARTICLES = ARTIFACT_ROOT / "reviewed-articles.tsv"
DEFAULT_OUTPUT = ARTIFACT_ROOT / "wiki-to-uat-bundle"
ARTICLE_ROOT = ROOT / "entities" / "article"
PERSISTENT_IDENTITY_MAP = ROOT / "index" / "wiki-uat-identity-map.tsv"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}
EXPECTED_WIKI_COUNT = 0
EXPECTED_INSERTS = 0
EXPECTED_EXISTING = 22_004
EXPECTED_LEGACY = 5_192
EXPECTED_SHARED = 16_812
EXPECTED_DIRECT_REVIEW = 0
EXPECTED_PRODUCTION_COUNT = 13_789
EXPECTED_PRODUCTION_CHECKSUMS = {
    "articles": 879_682_561,
    "article_coverage": 3_245_683_176,
    "article_media": 4_167_913_441,
    "article_tags": 1_981_695_764,
    "article_user_groups": 1_798_100_154,
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_id(value: Any) -> str:
    return str(value or "").replace("_", "-")


def _mysql_tsv_unescape(value: str) -> str:
    """Reverse the old stager's backslash escaping for one TSV scalar."""

    mapping = {"n": "\n", "r": "\r", "t": "\t", "0": "\x00", "b": "\b", "Z": "\x1a"}
    output = []
    index = 0
    while index < len(value):
        if value[index] != "\\" or index + 1 >= len(value):
            output.append(value[index])
            index += 1
            continue
        following = value[index + 1]
        output.append(mapping.get(following, following))
        index += 2
    return "".join(output)


def _plain(value: str) -> str:
    value = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", value)
    value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _section(body: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
        body,
    )
    return match.group(1).strip() if match else ""


def _catalog_titles() -> dict[str, str]:
    titles = {}
    catalog = ARTICLE_ROOT / "catalog.md"
    for line in catalog.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| ") or "sourceId" in line:
            continue
        parts = [part.strip() for part in line.strip("|").split("|")]
        if len(parts) >= 3:
            titles[parts[1]] = parts[2]
    return titles


def scan_wiki(expected_count: int | None = EXPECTED_WIKI_COUNT) -> dict[str, dict[str, Any]]:
    titles = _catalog_titles()
    notes = {}
    for path in sorted(ARTICLE_ROOT.glob("*/*.md")):
        if path.name in SYSTEM_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        frontmatter_raw, body_with_delimiter = split_note(text)
        metadata = yaml.safe_load(frontmatter_raw) or {}
        if not isinstance(metadata, dict):
            raise ProjectionError(f"invalid frontmatter mapping: {path.relative_to(ROOT)}")
        body = body_with_delimiter.lstrip("\n")
        source_id = str(metadata.get("sourceId") or "")
        if not source_id:
            raise ProjectionError(f"compiled note has no sourceId: {path.relative_to(ROOT)}")
        if source_id in notes:
            raise ProjectionError(f"duplicate wiki sourceId: {source_id}")
        source_body, rendered_projection = split_database_projection(body)
        projection = json.loads(rendered_projection) if rendered_projection else None
        h1 = re.search(r"(?m)^# (.+)$", source_body)
        title = _plain(h1.group(1)) if h1 else titles.get(source_id, "")
        if not title:
            title = title_from_slug(path.stem.removeprefix(source_id + "-"))
        notes[source_id] = {
            "path": path,
            "relativePath": path.relative_to(ROOT).as_posix(),
            "text": text,
            "textSha256": _sha256_text(text),
            "metadata": metadata,
            "body": source_body,
            "projection": projection,
            "title": title,
        }
    if expected_count is not None and len(notes) != expected_count:
        raise ProjectionError(
            f"wiki count differs: expected {expected_count}, found {len(notes)}"
        )
    return notes


def validate_wiki_tag_assignments(notes: dict[str, dict[str, Any]]) -> None:
    """Require every compiled projection tag to be one active canonical Tag entity."""

    records = load_tag_registry()
    active = {record.display_name for record in records if record.status == "active"}
    all_values = {record.display_name: record.status for record in records}
    for source_id, note in notes.items():
        projection = note.get("projection")
        if projection is None:
            continue
        for raw in projection.get("tags", []):
            value = str(raw).strip()
            if value not in all_values:
                raise ProjectionError(f"projected tag has no Tag entity for {source_id}: {value!r}")
            if value not in active:
                raise ProjectionError(
                    f"projected tag is not active for {source_id}: {value!r} ({all_values[value]})"
                )


def load_uat_identity() -> tuple[dict[str, list[dict[str, Any]]], dict[int, dict[str, Any]]]:
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_id = {}
    for line in IDENTITY_BASELINE.read_text(encoding="utf-8").splitlines():
        month, article_id_raw, source_id, origin = line.split("\t")
        article_id = int(article_id_raw)
        row = {
            "month": month,
            "uatArticleId": article_id,
            "logicalSourceId": source_id,
            "origin": origin,
        }
        by_source[source_id].append(row)
        by_id[article_id] = row
    if len(by_id) != EXPECTED_UAT_COUNT:
        raise ProjectionError("frozen UAT identity map does not contain 22,004 rows")
    return dict(by_source), by_id


def choose_existing_identity(source_id: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    feed = [row for row in candidates if row["origin"] != "legacy"]
    if len(feed) == 1:
        return feed[0]
    if len(candidates) == 1:
        return candidates[0]
    raise ProjectionError(f"ambiguous UAT identity for wiki sourceId {source_id}")


def _review_record(
    category: str,
    topic: str,
    tone: str,
    event_type: str,
    tags: Iterable[str],
    outlet_name: str,
    outlet_country: str,
    provenance: str,
) -> dict[str, Any]:
    return {
        "category": category or "Non-institutional",
        "topic": topic,
        "tone": tone or "Factual",
        "eventType": event_type or "Unfacilitated",
        "tags": canonical_multiset([str(tag) for tag in tags if str(tag).strip()]),
        "outletName": outlet_name,
        "outletCountry": outlet_country,
        "provenance": provenance,
    }


def load_reviews() -> dict[str, dict[str, Any]]:
    reviews: dict[str, dict[str, Any]] = {}
    review_paths = (JULY23_ARTICLES, JULY29_REVIEW, DIRECT_REVIEW)
    if not any(path.exists() for path in review_paths):
        return reviews
    if not all(path.exists() for path in review_paths):
        raise ProjectionError("configured review artifacts are incomplete")
    with JULY23_ARTICLES.open(encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle, delimiter="\t"):
            if len(row) != 32:
                raise ProjectionError("unexpected July 23 article staging row shape")
            source_id = _source_id(row[6])
            provenance = json.loads(_mysql_tsv_unescape(row[29]))
            values = provenance.get("approvedValues") or {}
            reviews[source_id] = _review_record(
                row[11],
                row[10],
                row[12],
                row[14],
                values.get("tags") or [],
                values.get("outletName") or "",
                values.get("outletCountry") or "",
                "runs/uat-projection/reviewed-articles.tsv",
            )

    july29 = json.loads(JULY29_REVIEW.read_text(encoding="utf-8"))
    for assessment in july29.get("assessments", []):
        source_id = _source_id(assessment["articleId"])
        values = assessment["consensus"]
        reviews[source_id] = _review_record(
            values.get("institutionalCategory") or "Non-institutional",
            "",
            values.get("tone") or "Factual",
            values.get("eventType") or "Unfacilitated",
            values.get("issueTags") or [],
            values.get("outletName") or "",
            values.get("outletCountry") or "",
            "runs/uat-projection/reviewed-enrichment.json",
        )

    direct = json.loads(DIRECT_REVIEW.read_text(encoding="utf-8"))
    direct_ids = []
    country_by_id = direct.get("outletCountryBySourceId", {})
    for group in direct.get("groups", []):
        for source_id in group["sourceIds"]:
            direct_ids.append(source_id)
            reviews[source_id] = _review_record(
                group["category"],
                group["topic"],
                group["tone"],
                group["eventType"],
                group["tags"],
                "",
                country_by_id.get(source_id, ""),
                "runs/uat-projection/direct-review.json",
            )
    if len(direct_ids) != EXPECTED_DIRECT_REVIEW or len(set(direct_ids)) != EXPECTED_DIRECT_REVIEW:
        raise ProjectionError("direct AI review artifact does not cover exactly 50 unique articles")
    return reviews


def _first_covered_by(body: str) -> str:
    section = _section(body, "Covered By")
    match = re.search(r"\[\[([^|\]]+)(?:\|([^\]]+))?\]\]", section)
    if not match:
        return ""
    display = match.group(2) or match.group(1).rsplit("/", 1)[-1]
    return title_from_slug(display) if display == display.lower() else display


def _source_description(note: dict[str, Any]) -> str:
    source = _section(note["body"], "Source Text")
    if source:
        return source
    summary = _section(note["body"], "Summary")
    if summary:
        return summary
    points = _section(note["body"], "Key Points")
    return points or note["title"]


def _mysql_datetime(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        match = re.match(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", text)
        if match:
            return f"{match.group(1)} {match.group(2)}"
        raise ProjectionError(f"invalid projected datetime: {value!r}")


DATETIME_FIELDS = (
    "published_date",
    "vendor_indexed_time",
    "indexed_date_time",
    "last_updated",
)


def _datetime_semantically_equal(expected: Any, observed: Any) -> bool:
    if expected is None or observed is None:
        return expected is observed
    try:
        left = datetime.fromisoformat(str(expected).replace("Z", "+00:00"))
        right = datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
    except ValueError:
        return False
    if left.tzinfo is not None:
        left = left.replace(tzinfo=None)
    if right.tzinfo is not None:
        right = right.replace(tzinfo=None)
    return left == right


def build_wiki_only_projection(
    source_id: str,
    note: dict[str, Any],
    review: dict[str, Any],
    article_id: int,
) -> dict[str, Any]:
    metadata = note["metadata"]
    published = _mysql_datetime(metadata.get("publishedDate"))
    created = _mysql_datetime(metadata.get("created")) or published
    title = note["title"]
    topic = review["topic"] or title
    outlet = review["outletName"] or _first_covered_by(note["body"])
    url = str(metadata.get("sourceUrl") or "")
    coverage = []
    if outlet or url or review["outletCountry"]:
        coverage.append({
            "coverage_id": None,
            "coverage_type": "online",
            "display_name": outlet or "Unknown Outlet",
            "country": review["outletCountry"] or None,
            "media_outlet_category": None,
            "url": url or None,
        })
    article = {
        "article_id": article_id,
        "document_id": article_id,
        "vendor_article_id": source_id,
        "article_title": title,
        "content_title": title,
        "content_description": _source_description(note),
        "topic": topic,
        "category": review["category"],
        "tone": review["tone"],
        "tone_sentiment": str(metadata.get("toneSentiment") or "Neutral"),
        "event_type": review["eventType"],
        "document_type_id": 2,
        "document_type_name": "Article",
        "product_type": "NEWS",
        "article_status": "A",
        "group_title": None,
        "news_type": str(metadata.get("sourceType") or "news"),
        "published_date": published,
        "vendor_indexed_time": published,
        "indexed_date_time": created or published,
        "last_updated": _mysql_datetime(metadata.get("last_updated")) or created or published,
        "uploaded_by": "wiki-projection",
        "last_updated_by": "wiki-projection",
    }
    return {
        "schemaVersion": PROJECTION_SCHEMA,
        "identity": {
            "wikiSourceId": source_id,
            "uatArticleId": article_id,
            "origin": "wiki",
        },
        "article": article,
        "coverage": canonical_multiset(coverage),
        "media": [],
        "tags": canonical_multiset(review["tags"]),
        "userGroups": [],
        "reviewProvenance": review["provenance"],
    }


def _projection_core(projection: dict[str, Any]) -> dict[str, Any]:
    return {
        "article": projection["article"],
        "coverage": canonical_multiset(projection.get("coverage", [])),
        "media": canonical_multiset(projection.get("media", [])),
        "tags": canonical_multiset(projection.get("tags", [])),
        "userGroups": canonical_multiset(projection.get("userGroups", [])),
    }


def _sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\")
    # Keep the SQL artifact line-stable while asking MySQL to reconstruct the
    # exact source control characters. This avoids universal-newline reads
    # changing a verified transaction before load.
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    text = text.replace("'", "''").replace("\x00", "")
    return "'" + text + "'"


def _insert_statements(table: str, columns: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    statements = []
    for start in range(0, len(rows), 100):
        values = []
        for row in rows[start : start + 100]:
            values.append("(" + ",".join(_sql_literal(value) for value in row) + ")")
        statements.append(
            f"INSERT INTO `{table}` ({','.join(f'`{column}`' for column in columns)}) VALUES\n"
            + ",\n".join(values)
            + ";"
        )
    return "\n".join(statements)


def render_load_sql(
    inserts: list[dict[str, Any]],
    bundle_id: str,
    start_count: int = EXPECTED_EXISTING,
    final_count: int = EXPECTED_WIKI_COUNT,
) -> str:
    start_message = (
        "UAT start count is not 22004"
        if start_count == EXPECTED_EXISTING
        else "UAT start count differs from verified bundle"
    )
    final_message = (
        "UAT final count is not 22820"
        if final_count == EXPECTED_WIKI_COUNT
        else "UAT final count differs from verified bundle"
    )
    parent_rows = [[projection["article"].get(column) for column in ARTICLE_COLUMNS] for projection in inserts]
    coverage_columns = [
        "article_id", "coverage_id", "coverage_type", "display_name",
        "country", "media_outlet_category", "url",
    ]
    coverage_rows = [
        [projection["article"]["article_id"], *[row.get(column) for column in coverage_columns[1:]]]
        for projection in inserts
        for row in projection["coverage"]
    ]
    media_columns = ["article_id", "media_id", "file_name", "media_url", "media_type", "source"]
    media_rows = [
        [projection["article"]["article_id"], *[row.get(column) for column in media_columns[1:]]]
        for projection in inserts
        for row in projection["media"]
    ]
    tag_rows = [
        [projection["article"]["article_id"], tag]
        for projection in inserts
        for tag in projection["tags"]
    ]
    group_rows = [
        [projection["article"]["article_id"], group_id]
        for projection in inserts
        for group_id in projection["userGroups"]
    ]
    procedure = "uat_load_wiki_union_" + re.sub(r"[^a-z0-9]", "", bundle_id.lower())[:16]
    body = "\n".join(filter(None, [
        _insert_statements("UAT_articles", ARTICLE_COLUMNS, parent_rows),
        _insert_statements("UAT_article_coverage", coverage_columns, coverage_rows),
        _insert_statements("UAT_article_media", media_columns, media_rows),
        _insert_statements("UAT_article_tags", ["article_id", "tag"], tag_rows),
        _insert_statements("UAT_article_user_groups", ["article_id", "user_group_id"], group_rows),
    ]))
    return f"""USE `{TARGET_DATABASE}`;
DROP PROCEDURE IF EXISTS `{procedure}`;
DELIMITER //
CREATE PROCEDURE `{procedure}`()
BEGIN
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;
  START TRANSACTION;
  IF (SELECT COUNT(*) FROM `UAT_articles`) <> {start_count} THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{start_message}';
  END IF;
{body}
  IF (SELECT COUNT(*) FROM `UAT_articles`) <> {final_count} THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{final_message}';
  END IF;
  IF EXISTS (
    SELECT 1 FROM `UAT_article_coverage` c
    LEFT JOIN `UAT_articles` a ON a.article_id=c.article_id
    WHERE a.article_id IS NULL
  ) OR EXISTS (
    SELECT 1 FROM `UAT_article_media` m
    LEFT JOIN `UAT_articles` a ON a.article_id=m.article_id
    WHERE a.article_id IS NULL
  ) OR EXISTS (
    SELECT 1 FROM `UAT_article_tags` t
    LEFT JOIN `UAT_articles` a ON a.article_id=t.article_id
    WHERE a.article_id IS NULL
  ) OR EXISTS (
    SELECT 1 FROM `UAT_article_user_groups` g
    LEFT JOIN `UAT_articles` a ON a.article_id=g.article_id
    WHERE a.article_id IS NULL
  ) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'UAT child orphan detected';
  END IF;
  COMMIT;
END//
DELIMITER ;
CALL `{procedure}`();
DROP PROCEDURE `{procedure}`;
"""


def render_rollback_sql(inserts: list[dict[str, Any]]) -> str:
    if not inserts:
        return f"""USE `{TARGET_DATABASE}`;
START TRANSACTION;
SELECT 0 AS deleted_articles;
SELECT COUNT(*) AS remaining_articles FROM `UAT_articles`;
ROLLBACK;
"""


def render_sync_sql(
    inserts: list[dict[str, Any]],
    updates: list[dict[str, Any]],
    bundle_id: str,
    start_count: int,
    final_count: int,
) -> str:
    """Render one rollback-on-failure UAT transaction for inserts and updates."""

    update_ids = sorted(int(value["article"]["article_id"]) for value in updates)
    delete_children = ""
    if update_ids:
        ids = ",".join(map(str, update_ids))
        delete_children = "\n".join(
            f"DELETE FROM `{table}` WHERE `article_id` IN ({ids});"
            for table in (
                "UAT_article_coverage", "UAT_article_media",
                "UAT_article_tags", "UAT_article_user_groups",
            )
        )
    parent_updates = []
    for projection in updates:
        article = projection["article"]
        article_id = int(article["article_id"])
        assignments = ",".join(
            f"`{column}`={_sql_literal(article.get(column))}"
            for column in ARTICLE_COLUMNS if column != "article_id"
        )
        parent_updates.append(
            f"UPDATE `UAT_articles` SET {assignments} WHERE `article_id`={article_id};"
        )
    all_children = [*inserts, *updates]
    coverage_columns = [
        "article_id", "coverage_id", "coverage_type", "display_name",
        "country", "media_outlet_category", "url",
    ]
    coverage_rows = [
        [projection["article"]["article_id"], *[row.get(column) for column in coverage_columns[1:]]]
        for projection in all_children for row in projection["coverage"]
    ]
    media_columns = ["article_id", "media_id", "file_name", "media_url", "media_type", "source"]
    media_rows = [
        [projection["article"]["article_id"], *[row.get(column) for column in media_columns[1:]]]
        for projection in all_children for row in projection["media"]
    ]
    tag_rows = [[p["article"]["article_id"], tag] for p in all_children for tag in p["tags"]]
    group_rows = [[p["article"]["article_id"], group] for p in all_children for group in p["userGroups"]]
    parent_rows = [[p["article"].get(column) for column in ARTICLE_COLUMNS] for p in inserts]
    body = "\n".join(filter(None, [
        delete_children,
        "\n".join(parent_updates),
        _insert_statements("UAT_articles", ARTICLE_COLUMNS, parent_rows),
        _insert_statements("UAT_article_coverage", coverage_columns, coverage_rows),
        _insert_statements("UAT_article_media", media_columns, media_rows),
        _insert_statements("UAT_article_tags", ["article_id", "tag"], tag_rows),
        _insert_statements("UAT_article_user_groups", ["article_id", "user_group_id"], group_rows),
    ]))
    procedure = "uat_sync_wiki_" + re.sub(r"[^a-z0-9]", "", bundle_id.lower())[:16]
    return f"""USE `{TARGET_DATABASE}`;
DROP PROCEDURE IF EXISTS `{procedure}`;
DELIMITER //
CREATE PROCEDURE `{procedure}`()
BEGIN
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;
  START TRANSACTION;
  IF (SELECT COUNT(*) FROM `UAT_articles`) <> {start_count} THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'UAT start count differs from verified update bundle';
  END IF;
{body}
  IF (SELECT COUNT(*) FROM `UAT_articles`) <> {final_count} THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'UAT final count differs from verified update bundle';
  END IF;
  COMMIT;
END//
DELIMITER ;
CALL `{procedure}`();
DROP PROCEDURE `{procedure}`;
"""
    ids = ",".join(str(value["article"]["article_id"]) for value in inserts)
    return f"""USE `{TARGET_DATABASE}`;
START TRANSACTION;
DELETE FROM `UAT_articles` WHERE `article_id` IN ({ids});
SELECT ROW_COUNT() AS deleted_articles;
SELECT COUNT(*) AS remaining_articles FROM `UAT_articles`;
-- Review both values before replacing the next statement with COMMIT.
ROLLBACK;
"""


def load_persistent_identity_map() -> dict[str, dict[str, Any]]:
    if not PERSISTENT_IDENTITY_MAP.is_file():
        raise ProjectionError(
            f"persistent identity map is missing: {PERSISTENT_IDENTITY_MAP.relative_to(ROOT)}"
        )
    rows = {}
    lines = PERSISTENT_IDENTITY_MAP.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != (
        "wiki_source_id\tuat_article_id\torigin\tmonth\tprojection_sha256"
    ):
        raise ProjectionError("persistent identity map header differs")
    for line in lines[1:]:
        source_id, article_id, origin, month, projection_sha = line.split("\t")
        if source_id in rows:
            raise ProjectionError(f"duplicate persistent source ID: {source_id}")
        rows[source_id] = {
            "uatArticleId": int(article_id),
            "origin": origin,
            "month": month,
            "projectionSha256": projection_sha,
        }
    return rows


def finalize_provisional_projection(
    source_id: str,
    projection: dict[str, Any],
    article_id: int,
) -> dict[str, Any]:
    value = json.loads(canonical_json(projection))
    if value.get("schemaVersion") != PROJECTION_SCHEMA:
        raise ProjectionError(f"new article has invalid projection schema: {source_id}")
    identity = value.get("identity") or {}
    if identity.get("wikiSourceId") != source_id or identity.get("origin") != "wiki":
        raise ProjectionError(f"new article has invalid provisional identity: {source_id}")
    existing_id = identity.get("uatArticleId")
    if existing_id not in (None, article_id):
        raise ProjectionError(f"new article carries a conflicting UAT ID: {source_id}")
    identity["uatArticleId"] = article_id
    value["identity"] = identity
    article = value.get("article") or {}
    if article.get("article_id") not in (None, article_id):
        raise ProjectionError(f"new article parent ID conflicts: {source_id}")
    article["article_id"] = article_id
    if article.get("document_id") is None:
        article["document_id"] = article_id
    for field in (
        "published_date",
        "vendor_indexed_time",
        "indexed_date_time",
        "last_updated",
    ):
        article[field] = _mysql_datetime(article.get(field))
    value["article"] = article
    value["coverage"] = canonical_multiset(value.get("coverage", []))
    value["media"] = canonical_multiset(value.get("media", []))
    value["tags"] = canonical_multiset(value.get("tags", []))
    value["userGroups"] = canonical_multiset(value.get("userGroups", []))
    validate_projection_shape(source_id, value)
    return value


def prepare_current(args: argparse.Namespace) -> int:
    """Prepare a reusable, rollback-capable insert/update delta from Markdown."""

    started = time.monotonic()
    notes = scan_wiki(expected_count=None)
    validate_wiki_tag_assignments(notes)
    persistent = load_persistent_identity_map()
    live = fetch_uat_projections()
    live_ids = set(live)
    mapped_ids = {row["uatArticleId"] for row in persistent.values()}
    if len(mapped_ids) != len(persistent):
        raise ProjectionError("persistent identity map is not one-to-one")
    if mapped_ids != live_ids:
        missing = sorted(mapped_ids - live_ids)
        extra = sorted(live_ids - mapped_ids)
        raise ProjectionError(
            f"persistent identity map and UAT differ; missing={missing[:10]}, extra={extra[:10]}"
        )
    removed = sorted(set(persistent) - set(notes))
    if removed:
        raise ProjectionError(
            f"wiki is missing {len(removed)} mapped UAT articles; first={removed[0]}"
        )
    new_source_ids = sorted(set(notes) - set(persistent))
    if not new_source_ids:
        max_id = max(live_ids) if live_ids else 0
    else:
        max_id = max(live_ids)

    preassigned = {}
    for source_id in new_source_ids:
        projection = notes[source_id]["projection"]
        if not projection:
            raise ProjectionError(f"new article lacks Database Projection: {source_id}")
        proposed_id = (projection.get("identity") or {}).get("uatArticleId")
        if proposed_id is not None:
            proposed_id = int(proposed_id)
            if proposed_id in live_ids or proposed_id <= max_id:
                raise ProjectionError(f"new article has unsafe preassigned UAT ID: {source_id}")
            if proposed_id in preassigned.values():
                raise ProjectionError(f"new articles share preassigned UAT ID {proposed_id}")
            preassigned[source_id] = proposed_id
    next_id = max([max_id, *preassigned.values()], default=max_id) + 1
    allocations = {}
    for source_id in new_source_ids:
        if source_id in preassigned:
            allocations[source_id] = preassigned[source_id]
        else:
            allocations[source_id] = next_id
            next_id += 1

    projections = {}
    update_source_ids = []
    for source_id, note in notes.items():
        if source_id in persistent:
            projection = note["projection"]
            if not projection:
                raise ProjectionError(f"mapped note lacks projection: {source_id}")
            expected_id = persistent[source_id]["uatArticleId"]
            identity_id = (projection.get("identity") or {}).get("uatArticleId")
            article_id = (projection.get("article") or {}).get("article_id")
            if identity_id is None and article_id is None:
                # A prior load may have committed the persistent mapping before
                # the allocated IDs were written back to Markdown. Recover the
                # exact live ID instead of treating the provisional note as a
                # malformed mapped projection.
                projection = finalize_provisional_projection(
                    source_id, projection, expected_id,
                )
            else:
                validate_projection_shape(source_id, projection)
                if int(projection["identity"]["uatArticleId"]) != expected_id:
                    raise ProjectionError(f"mapped note UAT ID differs: {source_id}")
            if _projection_core(projection) != _projection_core(live[expected_id]):
                update_source_ids.append(source_id)
            projections[source_id] = projection
        else:
            projections[source_id] = finalize_provisional_projection(
                source_id,
                note["projection"],
                allocations[source_id],
            )
    inserts = [projections[source_id] for source_id in new_source_ids]
    updates = [projections[source_id] for source_id in sorted(update_source_ids)]
    old_updates = []
    for source_id in sorted(update_source_ids):
        article_id = int(projections[source_id]["identity"]["uatArticleId"])
        old = json.loads(canonical_json(live[article_id]))
        old["identity"] = json.loads(canonical_json(projections[source_id]["identity"]))
        old_updates.append(old)
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ProjectionError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    projections_path = output / "projections.ndjson"
    identity_path = output / "identity-map.tsv"
    originals_path = output / "backfill-originals.ndjson.gz"
    delta_path = output / "delta.json"
    load_path = output / "load_uat.sql"
    rollback_path = output / "rollback_uat.sql"
    rollback_projections_path = output / "rollback-projections.ndjson"

    projection_lines = []
    identity_lines = ["wiki_source_id\tuat_article_id\torigin\tmonth\tprojection_sha256"]
    with gzip.open(originals_path, "wt", encoding="utf-8") as backup:
        for source_id in sorted(notes):
            note = notes[source_id]
            projection = projections[source_id]
            replace_provisional = note["projection"] != projection
            projection_lines.append(canonical_json({
                "relativePath": note["relativePath"],
                "sourceId": source_id,
                "originalNoteSha256": note["textSha256"],
                "projection": projection,
                "projectionSha256": record_hash(projection),
                "replaceProvisional": replace_provisional,
            }))
            identity_lines.append(
                f"{source_id}\t{projection['identity']['uatArticleId']}\t"
                f"{projection['identity']['origin']}\t{note['path'].parent.name}\t"
                f"{record_hash(projection)}"
            )
            if replace_provisional:
                backup.write(canonical_json({
                    "relativePath": note["relativePath"],
                    "originalNoteSha256": note["textSha256"],
                    "originalText": note["text"],
                }) + "\n")
    projections_path.write_text("\n".join(projection_lines) + "\n", encoding="utf-8")
    identity_path.write_text("\n".join(identity_lines) + "\n", encoding="utf-8")
    rollback_projections_path.write_text(
        "\n".join(canonical_json(value) for value in old_updates) + ("\n" if old_updates else ""),
        encoding="utf-8",
    )
    delta = {
        "inserts": len(inserts),
        "updates": len(updates),
        "deletes": 0,
        "insertArticleIds": [value["article"]["article_id"] for value in inserts],
        "updateArticleIds": [value["article"]["article_id"] for value in updates],
        "maxExistingArticleId": max_id,
        "parentRows": len(inserts),
        "coverageRows": sum(len(value["coverage"]) for value in inserts),
        "mediaRows": sum(len(value["media"]) for value in inserts),
        "tagRows": sum(len(value["tags"]) for value in inserts),
        "userGroupRows": sum(len(value["userGroups"]) for value in inserts),
    }
    delta_path.write_text(json.dumps(delta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    bundle_id = record_hash({
        "identity": identity_lines,
        "delta": delta,
        "rollbackProjectionHashes": [record_hash(value) for value in old_updates],
    })
    final_count = len(live) + len(inserts)
    load_path.write_text(
        render_sync_sql(inserts, updates, bundle_id, len(live), final_count),
        encoding="utf-8",
    )
    rollback_path.write_text(
        render_sync_sql([], old_updates, "rollback-" + bundle_id, final_count, len(live)),
        encoding="utf-8",
    )
    elapsed = time.monotonic() - started
    manifest = {
        "bundleVersion": "wiki-to-uat-current-bundle.v1",
        "bundleId": bundle_id,
        "targetDatabase": TARGET_DATABASE,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "wikiArticles": len(notes),
            "existingUatArticles": len(live),
            "projectionBackfills": sum(
                1 for source_id, note in notes.items()
                if note["projection"] != projections[source_id]
            ),
            "projectionAlreadyPresent": sum(
                1 for source_id, note in notes.items()
                if note["projection"] == projections[source_id]
            ),
        },
        "delta": delta,
        "timing": {
            "elapsedSeconds": round(elapsed, 6),
            "averageSecondsPerArticle": round(elapsed / len(notes), 9) if notes else None,
        },
    }
    write_hashed_manifest(
        output,
        manifest,
        [projections_path, identity_path, originals_path, delta_path, load_path,
         rollback_path, rollback_projections_path],
    )
    verify_bundle_dir(output)
    print(f"Prepared current delta for {len(notes)} wiki articles in {elapsed:.2f} seconds")
    print(f"Delta: {len(inserts)} inserts, {len(updates)} updates, 0 deletes")
    print(f"Bundle: {output.relative_to(ROOT)}")
    return 0


def prepare(args: argparse.Namespace) -> int:
    started = time.monotonic()
    verify_live_uat_baseline()
    notes = scan_wiki()
    validate_wiki_tag_assignments(notes)
    by_source, _ = load_uat_identity()
    reviews = load_reviews()
    reconciliation = json.loads(RECONCILIATION.read_text(encoding="utf-8"))
    expected_wiki_only = {row["sourceId"] for row in reconciliation["wikiOnly"]}

    shared_identities = {}
    for source_id in notes:
        if source_id.startswith("uat-legacy-"):
            continue
        if source_id in by_source:
            shared_identities[source_id] = choose_existing_identity(
                source_id, by_source[source_id]
            )
    if len(shared_identities) != EXPECTED_SHARED:
        raise ProjectionError(
            f"shared identity count differs: {len(shared_identities)}"
        )
    observed_wiki_only = set(notes) - set(shared_identities) - {
        source_id for source_id in notes if source_id.startswith("uat-legacy-")
    }
    if observed_wiki_only != expected_wiki_only:
        raise ProjectionError("current wiki-only identity set differs from frozen 816")
    if not observed_wiki_only <= set(reviews):
        missing = sorted(observed_wiki_only - set(reviews))
        raise ProjectionError(f"review evidence missing for {len(missing)} wiki-only articles")

    live_by_id = fetch_uat_projections(
        [row["uatArticleId"] for row in shared_identities.values()]
    )
    max_id = int(run_mysql("SELECT MAX(`article_id`) FROM `UAT_articles`;").strip())
    allocated = {
        source_id: max_id + offset
        for offset, source_id in enumerate(sorted(observed_wiki_only), start=1)
    }
    projections = {}
    origins = Counter()
    for source_id, note in notes.items():
        existing = note["projection"]
        if source_id.startswith("uat-legacy-"):
            if not existing:
                raise ProjectionError(f"legacy note lost projection: {source_id}")
            projection = existing
        elif source_id in shared_identities:
            identity = shared_identities[source_id]
            projection = live_by_id[identity["uatArticleId"]]
            projection["identity"] = {
                "wikiSourceId": source_id,
                "uatArticleId": identity["uatArticleId"],
                "origin": identity["origin"],
            }
        else:
            projection = build_wiki_only_projection(
                source_id,
                note,
                reviews[source_id],
                allocated[source_id],
            )
        if projection.get("schemaVersion") != PROJECTION_SCHEMA:
            raise ProjectionError(f"invalid projection schema for {source_id}")
        if projection["identity"]["wikiSourceId"] != source_id:
            raise ProjectionError(f"projection identity mismatch for {source_id}")
        projections[source_id] = projection
        origins[projection["identity"]["origin"]] += 1

    target_ids = [int(value["identity"]["uatArticleId"]) for value in projections.values()]
    if len(target_ids) != len(set(target_ids)):
        raise ProjectionError("projection assigns one UAT article ID more than once")
    inserts = [projections[source_id] for source_id in sorted(observed_wiki_only)]
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ProjectionError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    projections_path = output / "projections.ndjson"
    identity_path = output / "identity-map.tsv"
    originals_path = output / "backfill-originals.ndjson.gz"
    delta_path = output / "delta.json"
    load_path = output / "load_uat.sql"
    rollback_path = output / "rollback_uat.sql"

    projection_lines = []
    identity_lines = ["wiki_source_id\tuat_article_id\torigin\tmonth\tprojection_sha256"]
    with gzip.open(originals_path, "wt", encoding="utf-8") as backup:
        for source_id in sorted(notes):
            note = notes[source_id]
            projection = projections[source_id]
            projection_lines.append(canonical_json({
                "relativePath": note["relativePath"],
                "sourceId": source_id,
                "originalNoteSha256": note["textSha256"],
                "projection": projection,
                "projectionSha256": record_hash(projection),
            }))
            identity_lines.append(
                f"{source_id}\t{projection['identity']['uatArticleId']}\t"
                f"{projection['identity']['origin']}\t{note['path'].parent.name}\t"
                f"{record_hash(projection)}"
            )
            if note["projection"] is None:
                backup.write(canonical_json({
                    "relativePath": note["relativePath"],
                    "originalNoteSha256": note["textSha256"],
                    "originalText": note["text"],
                }) + "\n")
    projections_path.write_text("\n".join(projection_lines) + "\n", encoding="utf-8")
    identity_path.write_text("\n".join(identity_lines) + "\n", encoding="utf-8")
    delta = {
        "inserts": EXPECTED_INSERTS,
        "updates": 0,
        "deletes": 0,
        "insertArticleIds": [value["article"]["article_id"] for value in inserts],
        "maxExistingArticleId": max_id,
        "firstAllocatedArticleId": min(value["article"]["article_id"] for value in inserts),
        "lastAllocatedArticleId": max(value["article"]["article_id"] for value in inserts),
        "parentRows": len(inserts),
        "coverageRows": sum(len(value["coverage"]) for value in inserts),
        "mediaRows": sum(len(value["media"]) for value in inserts),
        "tagRows": sum(len(value["tags"]) for value in inserts),
        "userGroupRows": sum(len(value["userGroups"]) for value in inserts),
    }
    delta_path.write_text(json.dumps(delta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    bundle_id = record_hash({
        "identity": identity_lines,
        "delta": delta,
    })
    load_path.write_text(render_load_sql(inserts, bundle_id), encoding="utf-8")
    rollback_path.write_text(render_rollback_sql(inserts), encoding="utf-8")
    elapsed = time.monotonic() - started
    manifest = {
        "bundleVersion": "wiki-to-uat-bundle.v1",
        "bundleId": bundle_id,
        "targetDatabase": TARGET_DATABASE,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "wikiArticles": len(notes),
            "existingUatArticles": EXPECTED_EXISTING,
            "sharedArticles": EXPECTED_SHARED,
            "legacyImportedArticles": EXPECTED_LEGACY,
            "wikiOnlyArticles": EXPECTED_INSERTS,
            "directAiReviewedArticles": EXPECTED_DIRECT_REVIEW,
            "projectionBackfills": sum(note["projection"] is None for note in notes.values()),
            "projectionAlreadyPresent": sum(note["projection"] is not None for note in notes.values()),
        },
        "origins": dict(sorted(origins.items())),
        "delta": delta,
        "timing": {
            "elapsedSeconds": round(elapsed, 6),
            "averageSecondsPerArticle": round(elapsed / EXPECTED_WIKI_COUNT, 9),
        },
    }
    write_hashed_manifest(
        output,
        manifest,
        [projections_path, identity_path, originals_path, delta_path, load_path, rollback_path],
    )
    verify_bundle_dir(output)
    print(f"Prepared {len(notes)} wiki projections in {elapsed:.2f} seconds")
    print(f"Average: {elapsed / len(notes):.6f} seconds/article")
    print(f"Initial delta: {EXPECTED_INSERTS} inserts, 0 updates, 0 deletes")
    print(f"Bundle: {output.relative_to(ROOT)}")
    return 0


def load_projection_records(bundle_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (bundle_dir / "projections.ndjson").read_text(encoding="utf-8").splitlines()
        if line
    ]


def canonicalize_loaded_datetimes(args: argparse.Namespace) -> int:
    """Align approved inserted-note datetime strings with exact live UAT output."""

    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    manifest = verify_bundle_dir(bundle_dir)
    if manifest.get("bundleVersion") != "wiki-to-uat-bundle.v1":
        raise ProjectionError("datetime canonicalization requires the approved union bundle")
    insert_ids = {int(value) for value in manifest["delta"]["insertArticleIds"]}
    if len(insert_ids) != EXPECTED_INSERTS:
        raise ProjectionError("approved union bundle does not contain exactly 816 insert IDs")
    records = {
        int(record["projection"]["identity"]["uatArticleId"]): record
        for record in load_projection_records(bundle_dir)
        if int(record["projection"]["identity"]["uatArticleId"]) in insert_ids
    }
    if set(records) != insert_ids:
        raise ProjectionError("approved union bundle is missing inserted projection records")
    live = fetch_uat_projections(insert_ids)
    if set(live) != insert_ids:
        raise ProjectionError("live UAT is missing one or more approved inserted records")

    changes = []
    mismatch_fields = Counter()
    for article_id in sorted(insert_ids):
        record = records[article_id]
        expected = record["projection"]
        observed = live[article_id]
        expected_article = expected["article"]
        observed_article = observed["article"]
        non_datetime_expected = {
            key: value for key, value in expected_article.items()
            if key not in DATETIME_FIELDS
        }
        non_datetime_observed = {
            key: value for key, value in observed_article.items()
            if key not in DATETIME_FIELDS
        }
        if non_datetime_expected != non_datetime_observed:
            raise ProjectionError(
                f"non-datetime parent mismatch blocks canonicalization: {article_id}"
            )
        for section in ("coverage", "media", "tags", "userGroups"):
            if canonical_multiset(expected[section]) != canonical_multiset(observed[section]):
                raise ProjectionError(
                    f"{section} mismatch blocks canonicalization: {article_id}"
                )
        updated = json.loads(canonical_json(expected))
        for field in DATETIME_FIELDS:
            before = expected_article.get(field)
            after = observed_article.get(field)
            if not _datetime_semantically_equal(before, after):
                raise ProjectionError(
                    f"non-equivalent datetime blocks canonicalization: "
                    f"{article_id}/{field}: {before!r} != {after!r}"
                )
            if before != after:
                mismatch_fields[field] += 1
                updated["article"][field] = after
        path = ROOT / record["relativePath"]
        text = path.read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        _, rendered = split_database_projection(body)
        if not rendered or json.loads(rendered) != expected:
            raise ProjectionError(
                f"Markdown projection changed since approved bundle: {record['relativePath']}"
            )
        changes.append((path, text, updated))
    if len(changes) != EXPECTED_INSERTS:
        raise ProjectionError("datetime canonicalization did not preflight exactly 816 notes")
    if set(mismatch_fields) - set(DATETIME_FIELDS):
        raise ProjectionError("unexpected canonicalization field")

    backup_path = args.backup.resolve()
    report_path = args.output.resolve()
    if backup_path.exists() or report_path.exists():
        raise ProjectionError("canonicalization backup or report already exists")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(backup_path, "wt", encoding="utf-8") as handle:
        for path, text, _ in changes:
            handle.write(canonical_json({
                "relativePath": path.relative_to(ROOT).as_posix(),
                "sha256": _sha256_text(text),
                "originalText": text,
            }) + "\n")

    for path, text, updated in changes:
        match = DATABASE_PROJECTION_PATTERN.search(text)
        if not match:
            raise ProjectionError(f"projection disappeared during apply: {path}")
        replacement = "\n\n" + render_projection(updated).rstrip()
        canonical_text = text[: match.start()].rstrip() + replacement + "\n"
        path.write_text(canonical_text, encoding="utf-8")
    elapsed = time.monotonic() - started
    report = {
        "schemaVersion": "wiki-uat-datetime-canonicalization.v1",
        "approvedBy": args.approved_by,
        "approvalSource": "Codex task user message: approved",
        "sourceBundleId": manifest["bundleId"],
        "articleCount": len(changes),
        "updatedFieldCounts": dict(sorted(mismatch_fields.items())),
        "nonDatetimeParentMismatches": 0,
        "childMultisetMismatches": 0,
        "uatWrites": 0,
        "backup": backup_path.relative_to(ROOT).as_posix(),
        "backupSha256": sha256_file(backup_path),
        "elapsedSeconds": round(elapsed, 6),
        "averageSecondsPerArticle": round(elapsed / len(changes), 9),
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "status": "passed",
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Canonicalized {len(changes)} Markdown projections in {elapsed:.2f} seconds")
    print(f"Average: {elapsed / len(changes):.6f} seconds/article")
    print("UAT writes: 0")
    return 0


def validate_projection_shape(source_id: str, projection: dict[str, Any]) -> None:
    article = projection.get("article")
    if not isinstance(article, dict) or set(article) != set(ARTICLE_COLUMNS):
        raise ProjectionError(f"parent column set differs for {source_id}")
    identity = projection.get("identity") or {}
    if identity.get("wikiSourceId") != source_id:
        raise ProjectionError(f"identity source differs for {source_id}")
    if int(identity.get("uatArticleId")) != int(article["article_id"]):
        raise ProjectionError(f"parent and identity IDs differ for {source_id}")
    if not isinstance(article["article_id"], int) or article["article_id"] <= 0:
        raise ProjectionError(f"invalid article_id for {source_id}")
    if not isinstance(article["document_id"], int):
        raise ProjectionError(f"invalid document_id for {source_id}")

    limits = {
        "vendor_article_id": 400,
        "article_title": 500,
        "content_title": 500,
        "topic": 200,
        "category": 50,
        "tone": 20,
        "tone_sentiment": 20,
        "event_type": 20,
        "document_type_name": 50,
        "product_type": 20,
        "article_status": 1,
        "group_title": 300,
        "news_type": 100,
        "uploaded_by": 50,
        "last_updated_by": 50,
    }
    for field, limit in limits.items():
        value = article.get(field)
        if value is not None and len(str(value)) > limit:
            raise ProjectionError(
                f"{field} exceeds UAT limit {limit} for {source_id}"
            )
    coverage_keys = {
        "coverage_id", "coverage_type", "display_name", "country",
        "media_outlet_category", "url",
    }
    media_keys = {"media_id", "file_name", "media_url", "media_type", "source"}
    for row in projection.get("coverage", []):
        if not isinstance(row, dict) or set(row) != coverage_keys:
            raise ProjectionError(f"coverage column set differs for {source_id}")
        if row["coverage_type"] not in {"broadcast", "online", "print"}:
            raise ProjectionError(f"invalid coverage type for {source_id}")
        for field, limit in {
            "display_name": 200,
            "country": 100,
            "media_outlet_category": 100,
            "url": 1000,
        }.items():
            value = row.get(field)
            if value is not None and len(str(value)) > limit:
                raise ProjectionError(f"coverage {field} exceeds limit for {source_id}")
    for row in projection.get("media", []):
        if not isinstance(row, dict) or set(row) != media_keys:
            raise ProjectionError(f"media column set differs for {source_id}")
    if not all(isinstance(tag, str) and 0 < len(tag) <= 200 for tag in projection.get("tags", [])):
        raise ProjectionError(f"invalid tag value for {source_id}")
    if not all(isinstance(group_id, int) for group_id in projection.get("userGroups", [])):
        raise ProjectionError(f"invalid user group value for {source_id}")


def verify_bundle_dir(bundle_dir: Path) -> dict[str, Any]:
    manifest = verify_hashed_manifest(bundle_dir)
    if manifest.get("bundleVersion") == "wiki-to-uat-current-bundle.v1":
        return verify_current_bundle_dir(bundle_dir, manifest)
    if manifest.get("bundleVersion") != "wiki-to-uat-bundle.v1":
        raise ProjectionError("unexpected wiki-to-UAT bundle version")
    if manifest.get("targetDatabase") != TARGET_DATABASE:
        raise ProjectionError("bundle target is not AI_Animation_UAT")
    counts = manifest.get("counts", {})
    required_counts = {
        "wikiArticles": EXPECTED_WIKI_COUNT,
        "existingUatArticles": EXPECTED_EXISTING,
        "sharedArticles": EXPECTED_SHARED,
        "legacyImportedArticles": EXPECTED_LEGACY,
        "wikiOnlyArticles": EXPECTED_INSERTS,
        "directAiReviewedArticles": EXPECTED_DIRECT_REVIEW,
    }
    for key, value in required_counts.items():
        if counts.get(key) != value:
            raise ProjectionError(f"bundle count {key} differs: {counts.get(key)}")
    delta = manifest.get("delta", {})
    if (delta.get("inserts"), delta.get("updates"), delta.get("deletes")) != (816, 0, 0):
        raise ProjectionError("initial bundle delta is not exactly 816/0/0")

    records = load_projection_records(bundle_dir)
    if len(records) != EXPECTED_WIKI_COUNT:
        raise ProjectionError("projection bundle does not contain the configured record count")
    source_ids = set()
    article_ids = set()
    backfills = 0
    for record in records:
        projection = record["projection"]
        source_id = record["sourceId"]
        if projection.get("schemaVersion") != PROJECTION_SCHEMA:
            raise ProjectionError(f"invalid projection schema for {source_id}")
        validate_projection_shape(source_id, projection)
        if record["projectionSha256"] != record_hash(projection):
            raise ProjectionError(f"projection hash mismatch for {source_id}")
        article_id = int(projection["identity"]["uatArticleId"])
        if source_id in source_ids or article_id in article_ids:
            raise ProjectionError(f"non-unique projection identity: {source_id}/{article_id}")
        source_ids.add(source_id)
        article_ids.add(article_id)
        if projection["identity"]["origin"] != "uat-legacy":
            backfills += 1
    if len(article_ids) != EXPECTED_WIKI_COUNT:
        raise ProjectionError("identity map is not one-to-one")
    with gzip.open(bundle_dir / "backfill-originals.ndjson.gz", "rt", encoding="utf-8") as handle:
        backup_rows = [json.loads(line) for line in handle if line]
    backup_count = len(backup_rows)
    if backup_count != counts.get("projectionBackfills"):
        raise ProjectionError("backfill backup count differs from manifest")
    for row in backup_rows:
        if _sha256_text(row["originalText"]) != row["originalNoteSha256"]:
            raise ProjectionError(f"backfill original hash mismatch: {row['relativePath']}")

    identity_lines = (bundle_dir / "identity-map.tsv").read_text(encoding="utf-8").splitlines()
    if len(identity_lines) != EXPECTED_WIKI_COUNT + 1:
        raise ProjectionError("identity-map.tsv does not contain the configured data-row count")
    mapped = {}
    for line in identity_lines[1:]:
        source_id, article_id, origin, month, projection_sha = line.split("\t")
        mapped[source_id] = (int(article_id), origin, month, projection_sha)
    if len(mapped) != EXPECTED_WIKI_COUNT:
        raise ProjectionError("identity-map.tsv contains duplicate wiki source IDs")
    for record in records:
        source_id = record["sourceId"]
        expected = (
            int(record["projection"]["identity"]["uatArticleId"]),
            record["projection"]["identity"]["origin"],
            Path(record["relativePath"]).parent.name,
            record["projectionSha256"],
        )
        if mapped.get(source_id) != expected:
            raise ProjectionError(f"identity-map.tsv differs for {source_id}")

    inserts = [
        record["projection"]
        for record in records
        if record["projection"]["identity"]["origin"] == "wiki"
    ]
    if len(inserts) != EXPECTED_INSERTS:
        raise ProjectionError("bundle does not contain exactly 816 wiki-origin inserts")
    load_sql = (bundle_dir / "load_uat.sql").read_text(encoding="utf-8")
    expected_load_sql = render_load_sql(inserts, manifest["bundleId"])
    if load_sql != expected_load_sql:
        raise ProjectionError("load SQL is not the deterministic projection of the bundle")
    rollback_sql = (bundle_dir / "rollback_uat.sql").read_text(encoding="utf-8")
    if rollback_sql != render_rollback_sql(inserts):
        raise ProjectionError("rollback SQL is not the deterministic projection of the bundle")
    if f"USE `{TARGET_DATABASE}`;" not in load_sql or "START TRANSACTION;" not in load_sql:
        raise ProjectionError("load SQL lacks fixed UAT target or transaction")
    if "ROLLBACK;" not in load_sql or "SIGNAL SQLSTATE '45000'" not in load_sql:
        raise ProjectionError("load SQL lacks rollback-on-failure validation")
    if "AI_Animation`" in load_sql.replace(f"`{TARGET_DATABASE}`", ""):
        raise ProjectionError("load SQL mentions production database")
    return manifest


def verify_current_bundle_dir(
    bundle_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    if manifest.get("targetDatabase") != TARGET_DATABASE:
        raise ProjectionError("current bundle target is not AI_Animation_UAT")
    counts = manifest.get("counts", {})
    records = load_projection_records(bundle_dir)
    if len(records) != counts.get("wikiArticles"):
        raise ProjectionError("current bundle wiki count differs from records")
    delta = manifest.get("delta", {})
    if delta.get("deletes") != 0:
        raise ProjectionError("current bundle contains a prohibited article delete")
    insert_ids = [int(value) for value in delta.get("insertArticleIds", [])]
    update_ids = [int(value) for value in delta.get("updateArticleIds", [])]
    if len(insert_ids) != delta.get("inserts") or len(insert_ids) != len(set(insert_ids)):
        raise ProjectionError("current bundle insert identities differ from delta count")
    if len(update_ids) != delta.get("updates") or len(update_ids) != len(set(update_ids)):
        raise ProjectionError("current bundle update identities differ from delta count")
    if set(insert_ids) & set(update_ids):
        raise ProjectionError("current bundle insert and update identities overlap")

    source_ids = set()
    article_ids = set()
    by_article_id = {}
    for record in records:
        source_id = record["sourceId"]
        projection = record["projection"]
        validate_projection_shape(source_id, projection)
        if record["projectionSha256"] != record_hash(projection):
            raise ProjectionError(f"projection hash mismatch for {source_id}")
        article_id = int(projection["identity"]["uatArticleId"])
        if source_id in source_ids or article_id in article_ids:
            raise ProjectionError(f"current bundle identity is not unique: {source_id}")
        source_ids.add(source_id)
        article_ids.add(article_id)
        by_article_id[article_id] = projection
    if not set(insert_ids) <= article_ids:
        raise ProjectionError("current bundle insert IDs are absent from projections")
    if not set(update_ids) <= article_ids:
        raise ProjectionError("current bundle update IDs are absent from projections")

    with gzip.open(bundle_dir / "backfill-originals.ndjson.gz", "rt", encoding="utf-8") as handle:
        backups = [json.loads(line) for line in handle if line]
    if len(backups) != counts.get("projectionBackfills"):
        raise ProjectionError("current bundle provisional-backup count differs")
    for row in backups:
        if _sha256_text(row["originalText"]) != row["originalNoteSha256"]:
            raise ProjectionError(f"current original hash differs: {row['relativePath']}")

    identity_lines = (bundle_dir / "identity-map.tsv").read_text(encoding="utf-8").splitlines()
    if len(identity_lines) != len(records) + 1:
        raise ProjectionError("current identity map row count differs")
    if len({line.split("\t", 1)[0] for line in identity_lines[1:]}) != len(records):
        raise ProjectionError("current identity map has duplicate source IDs")

    inserts = [by_article_id[article_id] for article_id in insert_ids]
    updates = [by_article_id[article_id] for article_id in update_ids]
    rollback_projection_path = bundle_dir / "rollback-projections.ndjson"
    old_updates = _read_ndjson(rollback_projection_path)
    if len(old_updates) != len(update_ids):
        raise ProjectionError("rollback projection count differs from update count")
    if [int(value["article"]["article_id"]) for value in old_updates] != update_ids:
        raise ProjectionError("rollback projection identities differ from update delta")
    for value in old_updates:
        validate_projection_shape(str(value["identity"].get("wikiSourceId")), value)
    start_count = int(counts.get("existingUatArticles"))
    final_count = start_count + len(inserts)
    if (bundle_dir / "load_uat.sql").read_text(encoding="utf-8") != render_sync_sql(
        inserts, updates, manifest["bundleId"], start_count, final_count,
    ):
        raise ProjectionError("current load SQL differs from deterministic projection")
    if (bundle_dir / "rollback_uat.sql").read_text(encoding="utf-8") != render_sync_sql(
        [], old_updates, "rollback-" + manifest["bundleId"], final_count, start_count,
    ):
        raise ProjectionError("current rollback SQL differs from deterministic projection")
    return manifest


def verify(args: argparse.Namespace) -> int:
    started = time.monotonic()
    manifest = verify_bundle_dir(args.bundle_dir.resolve())
    elapsed = time.monotonic() - started
    print(f"Verified {manifest['counts']['wikiArticles']} projections in {elapsed:.2f} seconds")
    print(f"Average: {elapsed / EXPECTED_WIKI_COUNT:.6f} seconds/article")
    return 0


def apply_projections(args: argparse.Namespace) -> int:
    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    manifest = verify_bundle_dir(bundle_dir)
    records = load_projection_records(bundle_dir)
    pending = []
    for record in records:
        path = ROOT / record["relativePath"]
        text = path.read_text(encoding="utf-8")
        if _sha256_text(text) != record["originalNoteSha256"]:
            _, body = parse_frontmatter(text)
            _, rendered = split_database_projection(body)
            if rendered and json.loads(rendered) == record["projection"]:
                continue
            raise ProjectionError(f"note changed after bundle preparation: {record['relativePath']}")
        _, body = parse_frontmatter(text)
        _, rendered = split_database_projection(body)
        if rendered:
            if json.loads(rendered) != record["projection"]:
                current = json.loads(rendered)
                if not record.get("replaceProvisional"):
                    raise ProjectionError(f"existing projection differs: {record['relativePath']}")
                if (current.get("identity") or {}).get("uatArticleId") is not None:
                    raise ProjectionError(
                        f"refusing to replace allocated projection: {record['relativePath']}"
                    )
                pending.append((path, text, record["projection"], "replace"))
                continue
            continue
        pending.append((path, text, record["projection"], "append"))
    expected_backfills = manifest["counts"]["projectionBackfills"]
    if len(pending) > expected_backfills:
        raise ProjectionError(
            f"pending backfill count exceeds bundle contract: expected at most "
            f"{expected_backfills}, found {len(pending)}"
        )
    for path, text, projection, action in pending:
        if action == "append":
            updated = text.rstrip() + "\n\n" + render_projection(projection)
        else:
            match = DATABASE_PROJECTION_PATTERN.search(text)
            if not match:
                raise ProjectionError(f"provisional projection disappeared: {path}")
            updated = text[: match.start()].rstrip() + "\n\n" + render_projection(projection)
        path.write_text(updated, encoding="utf-8")
    elapsed = time.monotonic() - started
    print(f"Applied {len(pending)} projection backfills in {elapsed:.2f} seconds")
    if pending:
        print(f"Average: {elapsed / len(pending):.6f} seconds/article")
    return 0


def compute_diff(bundle_dir: Path) -> dict[str, Any]:
    manifest = verify_bundle_dir(bundle_dir)
    records = load_projection_records(bundle_dir)
    projected_by_id = {
        int(record["projection"]["identity"]["uatArticleId"]): record["projection"]
        for record in records
    }
    live = fetch_uat_projections()
    inserts = sorted(set(projected_by_id) - set(live))
    deletes = sorted(set(live) - set(projected_by_id))
    updates = []
    mismatch_samples = []
    for article_id in sorted(set(projected_by_id) & set(live)):
        expected = _projection_core(projected_by_id[article_id])
        observed = _projection_core(live[article_id])
        if expected != observed:
            updates.append(article_id)
            if len(mismatch_samples) < 20:
                mismatch_samples.append({
                    "articleId": article_id,
                    "expectedHash": record_hash(expected),
                    "observedHash": record_hash(observed),
                })
    return {
        "bundleId": manifest["bundleId"],
        "targetDatabase": TARGET_DATABASE,
        "liveCount": len(live),
        "projectedCount": len(projected_by_id),
        "inserts": len(inserts),
        "updates": len(updates),
        "deletes": len(deletes),
        "insertArticleIds": inserts,
        "updateArticleIds": updates,
        "deleteArticleIds": deletes,
        "mismatchSamples": mismatch_samples,
    }


def diff(args: argparse.Namespace) -> int:
    started = time.monotonic()
    result = compute_diff(args.bundle_dir.resolve())
    elapsed = time.monotonic() - started
    result["elapsedSeconds"] = round(elapsed, 6)
    result["averageSecondsPerArticle"] = round(elapsed / EXPECTED_WIKI_COUNT, 9)
    if args.output:
        args.output.resolve().write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    display = {
        key: value
        for key, value in result.items()
        if key not in {"insertArticleIds", "updateArticleIds", "deleteArticleIds"}
    }
    display["insertArticleIdSample"] = result["insertArticleIds"][:10]
    display["updateArticleIdSample"] = result["updateArticleIds"][:10]
    display["deleteArticleIdSample"] = result["deleteArticleIds"][:10]
    print(json.dumps(display, indent=2, sort_keys=True))
    manifest = verify_bundle_dir(args.bundle_dir.resolve())
    expected = (
        (0, 0, 0)
        if args.expect_zero
        else (
            int(manifest["delta"]["inserts"]),
            int(manifest["delta"]["updates"]),
            int(manifest["delta"]["deletes"]),
        )
    )
    observed = (result["inserts"], result["updates"], result["deletes"])
    if observed != expected:
        raise ProjectionError(f"diff is {observed}, expected {expected}")
    return 0


def _verify_approval(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    approval = json.loads(path.read_text(encoding="utf-8"))
    required = ["approvedBy", "approvedAt", "bundleId", "approved"]
    if any(key not in approval for key in required):
        raise ProjectionError("approval file lacks required attributed fields")
    if approval["approved"] is not True:
        raise ProjectionError("UAT transaction is not approved")
    if not str(approval["approvedBy"]).strip() or not str(approval["approvedAt"]).strip():
        raise ProjectionError("approval must identify approver and timestamp")
    if approval["bundleId"] != manifest["bundleId"]:
        raise ProjectionError("approval is not bound to this verified bundle")
    return approval


def load(args: argparse.Namespace) -> int:
    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    manifest = verify_bundle_dir(bundle_dir)
    _verify_approval(args.approval_file.resolve(), manifest)
    before = compute_diff(bundle_dir)
    expected_delta = (
        int(manifest["delta"]["inserts"]),
        int(manifest["delta"]["updates"]),
        int(manifest["delta"]["deletes"]),
    )
    if (before["inserts"], before["updates"], before["deletes"]) != expected_delta:
        raise ProjectionError(
            f"pre-load diff differs from verified bundle: "
            f"{(before['inserts'], before['updates'], before['deletes'])} "
            f"!= {expected_delta}"
        )
    sql = (bundle_dir / "load_uat.sql").read_text(encoding="utf-8")
    run_mysql(sql, write=True)
    after = compute_diff(bundle_dir)
    if (after["inserts"], after["updates"], after["deletes"]) != (0, 0, 0):
        raise ProjectionError("post-load UAT does not match the wiki projection")
    PERSISTENT_IDENTITY_MAP.parent.mkdir(parents=True, exist_ok=True)
    PERSISTENT_IDENTITY_MAP.write_text(
        (bundle_dir / "identity-map.tsv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    elapsed = time.monotonic() - started
    inserted_count = int(manifest["delta"]["inserts"])
    receipt = {
        "schemaVersion": "wiki-uat-load-receipt.v1",
        "bundleId": manifest["bundleId"],
        "approval": json.loads(args.approval_file.read_text(encoding="utf-8")),
        "before": before,
        "after": after,
        "elapsedSeconds": round(elapsed, 6),
        "averageSecondsPerInsertedArticle": (
            round(elapsed / inserted_count, 9) if inserted_count else None
        ),
        "loadedAt": datetime.now(timezone.utc).isoformat(),
    }
    receipt_path = bundle_dir.parent / f"{bundle_dir.name}-load-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Loaded {inserted_count} UAT articles in {elapsed:.2f} seconds")
    if inserted_count:
        print(f"Average: {elapsed / inserted_count:.6f} seconds/article")
    return 0


TOPIC_DELTA_SCHEMA = "wiki-uat-topic-delta.v1"
TOPIC_LOAD_SCHEMA = "wiki-uat-topic-load.v1"


def _read_ndjson(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def verify_topic_load_bundle_dir(bundle_dir: Path) -> dict[str, Any]:
    manifest = verify_hashed_manifest(bundle_dir)
    if manifest.get("schemaVersion") != TOPIC_LOAD_SCHEMA:
        raise ProjectionError("unexpected topic-load bundle schema")
    if manifest.get("targetDatabase") != TARGET_DATABASE or manifest.get("loadPermitted") is not True:
        raise ProjectionError("topic-load bundle is not explicitly authorized for UAT")
    rows = _read_ndjson(bundle_dir / "topic-updates.ndjson")
    counts = manifest.get("counts") or {}
    if len(rows) != int(counts.get("topicUpdates", -1)):
        raise ProjectionError("topic-load row count differs from manifest")
    if any(int(counts.get(key, -1)) != 0 for key in ("inserts", "deletes", "childChanges", "unauthorizedParentChanges")):
        raise ProjectionError("topic-load bundle includes a forbidden difference class")
    expected_keys = {"articleId", "sourceId", "articlePath", "fromTopic", "toTopic"}
    ids = []
    for row in rows:
        if set(row) != expected_keys or row["fromTopic"] == row["toTopic"]:
            raise ProjectionError("topic-load row is malformed or unchanged")
        ids.append(int(row["articleId"]))
    if len(ids) != len(set(ids)):
        raise ProjectionError("topic-load bundle contains duplicate article IDs")
    expected_id = record_hash({
        "sourceDeltaManifestSha256": manifest["sourceDeltaManifestSha256"],
        "topicUpdatesSha256": sha256_file(bundle_dir / "topic-updates.ndjson"),
        "topicUpdates": len(rows),
    })
    if manifest.get("bundleId") != expected_id:
        raise ProjectionError("topic-load bundle ID is not deterministic")
    return manifest


def prepare_topic_load(args: argparse.Namespace) -> int:
    source = args.delta_dir.resolve()
    source_manifest = verify_hashed_manifest(source)
    if source_manifest.get("schemaVersion") != TOPIC_DELTA_SCHEMA:
        raise ProjectionError("source is not a verified topic delta")
    if source_manifest.get("targetDatabase") != TARGET_DATABASE or source_manifest.get("loadPermitted") is not False:
        raise ProjectionError("source topic delta trust boundary differs")
    counts = source_manifest.get("counts") or {}
    if any(int(counts.get(key, -1)) != 0 for key in ("inserts", "deletes", "childChanges", "unauthorizedParentChanges")):
        raise ProjectionError("source topic delta contains forbidden differences")
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ProjectionError(f"output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    updates = output / "topic-updates.ndjson"
    shutil.copyfile(source / "topic-updates.ndjson", updates)
    source_sha = sha256_file(source / "bundle_manifest.json")
    bundle_id = record_hash({
        "sourceDeltaManifestSha256": source_sha,
        "topicUpdatesSha256": sha256_file(updates),
        "topicUpdates": int(counts["topicUpdates"]),
    })
    manifest = {
        "schemaVersion": TOPIC_LOAD_SCHEMA,
        "bundleId": bundle_id,
        "targetDatabase": TARGET_DATABASE,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "loadPermitted": True,
        "sourceDeltaManifestSha256": source_sha,
        "counts": counts,
    }
    write_hashed_manifest(output, manifest, [updates])
    verify_topic_load_bundle_dir(output)
    print(json.dumps({"bundle": output.relative_to(ROOT).as_posix(), "bundleId": bundle_id, **counts, "verified": True}, indent=2))
    return 0


def _topic_rows_from_uat(article_ids: Iterable[int]) -> dict[int, Any]:
    ids = sorted({int(value) for value in article_ids})
    if not ids:
        return {}
    where = ",".join(str(value) for value in ids)
    output = run_mysql(
        "SELECT `article_id`,IF(`topic` IS NULL,'N',CONCAT('H',HEX(`topic`))) "
        f"FROM `UAT_articles` WHERE `article_id` IN ({where}) ORDER BY `article_id`;\n"
    )
    rows = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        article_id_raw, encoded = line.split("\t", 1)
        rows[int(article_id_raw)] = (
            None if encoded == "N" else bytes.fromhex(encoded.removeprefix("H")).decode("utf-8")
        )
    return rows


def _render_topic_update_sql(rows: list[dict[str, Any]], bundle_id: str) -> str:
    procedure = "uat_topic_sync_" + bundle_id[:16]
    temporary = "tmp_topic_sync_" + bundle_id[:12]
    values = _insert_statements(
        temporary,
        ["article_id", "old_topic", "new_topic"],
        [[int(row["articleId"]), row["fromTopic"], row["toTopic"]] for row in rows],
    )
    expected = len(rows)
    return f"""USE `{TARGET_DATABASE}`;
DROP PROCEDURE IF EXISTS `{procedure}`;
DELIMITER //
CREATE PROCEDURE `{procedure}`()
BEGIN
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;
  START TRANSACTION;
  CREATE TEMPORARY TABLE `{temporary}` (
    `article_id` BIGINT PRIMARY KEY,
    `old_topic` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL,
    `new_topic` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL
  );
{values}
  IF (SELECT COUNT(*) FROM `{temporary}`) <> {expected} THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'topic delta row count differs';
  END IF;
  IF EXISTS (
    SELECT 1 FROM `{temporary}` d
    LEFT JOIN `UAT_articles` a ON a.`article_id` = d.`article_id`
    WHERE a.`article_id` IS NULL OR NOT (a.`topic` <=> d.`old_topic`)
  ) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'live UAT topic precondition differs';
  END IF;
  UPDATE `UAT_articles` a
  JOIN `{temporary}` d ON d.`article_id` = a.`article_id`
  SET a.`topic` = d.`new_topic`;
  IF ROW_COUNT() <> {expected} THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'updated topic count differs';
  END IF;
  IF EXISTS (
    SELECT 1 FROM `{temporary}` d
    JOIN `UAT_articles` a ON a.`article_id` = d.`article_id`
    WHERE NOT (a.`topic` <=> d.`new_topic`)
  ) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'post-update topic verification differs';
  END IF;
  COMMIT;
  DROP TEMPORARY TABLE `{temporary}`;
END//
DELIMITER ;
CALL `{procedure}`();
DROP PROCEDURE `{procedure}`;
"""


def load_topic_updates(args: argparse.Namespace) -> int:
    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    manifest = verify_topic_load_bundle_dir(bundle_dir)
    approval = _verify_approval(args.approval_file.resolve(), manifest)
    rows = _read_ndjson(bundle_dir / "topic-updates.ndjson")
    expected_ids = {int(row["articleId"]) for row in rows}
    live_before = _topic_rows_from_uat(expected_ids)
    if set(live_before) != expected_ids:
        raise ProjectionError("live UAT identity set differs from approved topic bundle")
    precondition_failures = [
        int(row["articleId"]) for row in rows
        if live_before.get(int(row["articleId"])) != row["fromTopic"]
    ]
    if precondition_failures:
        raise ProjectionError(f"live UAT topics changed after approval; first={precondition_failures[:10]}")
    run_mysql(_render_topic_update_sql(rows, manifest["bundleId"]), write=True)
    live_after = _topic_rows_from_uat(expected_ids)
    post_failures = [
        int(row["articleId"]) for row in rows
        if live_after.get(int(row["articleId"])) != row["toTopic"]
    ]
    if post_failures or set(live_after) != expected_ids:
        raise ProjectionError(f"post-load topic verification differs; first={post_failures[:10]}")
    elapsed = time.monotonic() - started
    receipt = {
        "schemaVersion": "wiki-uat-topic-load-receipt.v1",
        "bundleId": manifest["bundleId"],
        "approval": approval,
        "targetDatabase": TARGET_DATABASE,
        "topicUpdates": len(rows),
        "beforeMismatches": 0,
        "afterMismatches": 0,
        "loadedAt": datetime.now(timezone.utc).isoformat(),
        "elapsedSeconds": round(elapsed, 6),
    }
    receipt_path = bundle_dir.parent / f"{bundle_dir.name}-load-receipt.json"
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({**receipt, "receipt": receipt_path.relative_to(ROOT).as_posix()}, indent=2))
    return 0


def verify_topic_sync(args: argparse.Namespace) -> int:
    bundle_dir = args.bundle_dir.resolve()
    manifest = verify_topic_load_bundle_dir(bundle_dir)
    rows = _read_ndjson(bundle_dir / "topic-updates.ndjson")
    expected_ids = {int(row["articleId"]) for row in rows}
    live = _topic_rows_from_uat(expected_ids)
    expected_topics = {row["toTopic"] for row in rows}
    live_topics = set(live.values())
    old_matches = sum(live.get(int(row["articleId"])) == row["fromTopic"] for row in rows)
    new_matches = sum(live.get(int(row["articleId"])) == row["toTopic"] for row in rows)
    result = {
        "bundleId": manifest["bundleId"],
        "targetDatabase": TARGET_DATABASE,
        "liveArticles": len(live),
        "approvedRows": len(rows),
        "identitySetMatches": set(live) == expected_ids,
        "oldTopicMatches": old_matches,
        "newTopicMatches": new_matches,
        "otherTopicMismatches": len(rows) - old_matches - new_matches,
        "expectedDistinctTopics": len(expected_topics),
        "liveDistinctTopics": len(live_topics),
        "topicSetMatches": live_topics == expected_topics,
        "synchronized": set(live) == expected_ids and new_matches == len(rows) and live_topics == expected_topics,
    }
    print(json.dumps(result, indent=2))
    if not result["synchronized"]:
        raise ProjectionError("UAT topics do not exactly match the approved wiki topic bundle")
    return 0


def verify_production(args: argparse.Namespace) -> int:
    started = time.monotonic()
    count = int(run_production_readonly("SELECT COUNT(*) FROM `articles`;").strip())
    tables = ",".join(f"`{table}`" for table in EXPECTED_PRODUCTION_CHECKSUMS)
    observed = {}
    for line in run_production_readonly(f"CHECKSUM TABLE {tables};").splitlines():
        name, checksum = line.split("\t")
        observed[name.rsplit(".", 1)[-1]] = int(checksum)
    if count != EXPECTED_PRODUCTION_COUNT:
        raise ProjectionError(
            f"production count changed: expected {EXPECTED_PRODUCTION_COUNT}, found {count}"
        )
    if observed != EXPECTED_PRODUCTION_CHECKSUMS:
        raise ProjectionError(
            f"production checksums changed: expected {EXPECTED_PRODUCTION_CHECKSUMS}, "
            f"found {observed}"
        )
    elapsed = time.monotonic() - started
    result = {
        "database": "AI_Animation",
        "readOnly": True,
        "articleCount": count,
        "checksums": observed,
        "elapsedSeconds": round(elapsed, 6),
        "status": "unchanged",
    }
    if args.output:
        args.output.resolve().write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def verify_rollback(args: argparse.Namespace) -> int:
    """Test rollback evidence without executing a database write."""

    started = time.monotonic()
    bundle_dir = args.bundle_dir.resolve()
    manifest = verify_bundle_dir(bundle_dir)
    records = load_projection_records(bundle_dir)
    insert_ids = [int(value) for value in manifest["delta"]["insertArticleIds"]]
    if len(insert_ids) != EXPECTED_INSERTS:
        raise ProjectionError("rollback test requires the 816-insert union bundle")
    by_id = {
        int(record["projection"]["identity"]["uatArticleId"]): record["projection"]
        for record in records
    }
    inserts = [by_id[article_id] for article_id in insert_ids]
    rollback_sql = (bundle_dir / "rollback_uat.sql").read_text(encoding="utf-8")
    if rollback_sql != render_rollback_sql(inserts):
        raise ProjectionError("rollback SQL differs from the verified bundle projection")
    if "START TRANSACTION;" not in rollback_sql or not rollback_sql.rstrip().endswith("ROLLBACK;"):
        raise ProjectionError("rollback SQL lacks transaction boundaries")

    live = fetch_uat_projections(insert_ids)
    if set(live) != set(insert_ids):
        raise ProjectionError("rollback live snapshot does not contain all 816 inserted IDs")
    before_hashes = {article_id: record_hash(_projection_core(value)) for article_id, value in live.items()}
    simulated = dict(live)
    for article_id in insert_ids:
        simulated.pop(article_id)
    if simulated:
        raise ProjectionError("in-memory rollback delete simulation left inserted rows")
    simulated.update(live)
    after_hashes = {
        article_id: record_hash(_projection_core(value))
        for article_id, value in simulated.items()
    }
    if before_hashes != after_hashes:
        raise ProjectionError("in-memory rollback restore did not reproduce the snapshot")

    fk_output = run_mysql(
        "SELECT TABLE_NAME,DELETE_RULE "
        "FROM information_schema.REFERENTIAL_CONSTRAINTS "
        "WHERE CONSTRAINT_SCHEMA='AI_Animation_UAT' "
        "AND REFERENCED_TABLE_NAME='UAT_articles' "
        "ORDER BY TABLE_NAME;"
    )
    fk_rules = {}
    for line in fk_output.splitlines():
        table, rule = line.split("\t")
        fk_rules[table] = rule
    expected_fk_rules = {
        "UAT_article_coverage": "CASCADE",
        "UAT_article_media": "CASCADE",
        "UAT_article_tags": "CASCADE",
        "UAT_article_user_groups": "CASCADE",
    }
    if fk_rules != expected_fk_rules:
        raise ProjectionError(
            f"rollback foreign-key rules differ: expected {expected_fk_rules}, found {fk_rules}"
        )
    elapsed = time.monotonic() - started
    report = {
        "schemaVersion": "wiki-uat-rollback-verification.v1",
        "bundleId": manifest["bundleId"],
        "rollbackSqlSha256": sha256_file(bundle_dir / "rollback_uat.sql"),
        "liveSnapshotArticles": len(live),
        "foreignKeyDeleteRules": fk_rules,
        "simulatedDeleteArticles": len(insert_ids),
        "simulatedRestoreHashMatch": True,
        "databaseWrites": 0,
        "elapsedSeconds": round(elapsed, 6),
        "averageSecondsPerArticle": round(elapsed / len(insert_ids), 9),
        "status": "passed",
    }
    args.output.resolve().write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    prepare_parser.set_defaults(func=prepare)
    current_parser = commands.add_parser("prepare-current")
    current_parser.add_argument("--output-dir", type=Path, required=True)
    current_parser.set_defaults(func=prepare_current)
    canonicalize_parser = commands.add_parser("canonicalize-loaded-datetimes")
    canonicalize_parser.add_argument("--bundle-dir", type=Path, required=True)
    canonicalize_parser.add_argument("--backup", type=Path, required=True)
    canonicalize_parser.add_argument("--output", type=Path, required=True)
    canonicalize_parser.add_argument("--approved-by", default="Christopher")
    canonicalize_parser.set_defaults(func=canonicalize_loaded_datetimes)
    verify_parser = commands.add_parser("verify-bundle")
    verify_parser.add_argument("--bundle-dir", type=Path, required=True)
    verify_parser.set_defaults(func=verify)
    apply_parser = commands.add_parser("apply-projections")
    apply_parser.add_argument("--bundle-dir", type=Path, required=True)
    apply_parser.set_defaults(func=apply_projections)
    diff_parser = commands.add_parser("diff")
    diff_parser.add_argument("--bundle-dir", type=Path, required=True)
    diff_parser.add_argument("--expect-zero", action="store_true")
    diff_parser.add_argument("--output", type=Path)
    diff_parser.set_defaults(func=diff)
    load_parser = commands.add_parser("load")
    load_parser.add_argument("--bundle-dir", type=Path, required=True)
    load_parser.add_argument("--approval-file", type=Path, required=True)
    load_parser.set_defaults(func=load)
    topic_prepare_parser = commands.add_parser("prepare-topic-load")
    topic_prepare_parser.add_argument("--delta-dir", type=Path, required=True)
    topic_prepare_parser.add_argument("--output-dir", type=Path, required=True)
    topic_prepare_parser.set_defaults(func=prepare_topic_load)
    topic_load_parser = commands.add_parser("load-topic-updates")
    topic_load_parser.add_argument("--bundle-dir", type=Path, required=True)
    topic_load_parser.add_argument("--approval-file", type=Path, required=True)
    topic_load_parser.set_defaults(func=load_topic_updates)
    topic_verify_parser = commands.add_parser("verify-topic-sync")
    topic_verify_parser.add_argument("--bundle-dir", type=Path, required=True)
    topic_verify_parser.set_defaults(func=verify_topic_sync)
    production_parser = commands.add_parser("verify-production")
    production_parser.add_argument("--output", type=Path)
    production_parser.set_defaults(func=verify_production)
    rollback_parser = commands.add_parser("verify-rollback")
    rollback_parser.add_argument("--bundle-dir", type=Path, required=True)
    rollback_parser.add_argument("--output", type=Path, required=True)
    rollback_parser.set_defaults(func=verify_rollback)
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
