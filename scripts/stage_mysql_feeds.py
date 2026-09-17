#!/usr/bin/env python3
"""HISTORICAL ONLY: prepare the superseded raw-feed staging bundle.

Do not use this script for new database loads. Compiled Markdown is now the sole
source of truth and ``project_wiki_to_uat.py`` is the only active UAT projector.
This file remains available solely to reproduce older run artifacts.

It never edits files under raw/ and never targets canonical UAT tables for writes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
LOADER_VERSION = "phase3-stager.v1"
TARGET_DATABASE = "AI_Animation_UAT"

EXPECTED_TOP_LEVEL_FIELDS = {
    "articleHeroImage",
    "articleId",
    "articleStatus",
    "articleTitle",
    "category",
    "contentDescription",
    "contentTitle",
    "documentId",
    "documentTypeId",
    "documentTypeName",
    "eventType",
    "groupTitle",
    "indexedDateTime",
    "lastUpdated",
    "lastUpdatedBy",
    "listOfCoverageBroadcast",
    "listOfCoverageOnline",
    "listOfCoveragePrints",
    "listOfMedia",
    "listOfSentiment",
    "listOfTags",
    "listOfUserGroupId",
    "newsType",
    "productType",
    "publishedDate",
    "tone",
    "toneSentiment",
    "topic",
    "uploadedBy",
    "vendorArticleId",
    "vendorIndexedTime",
}

COVERAGE_FIELDS = {"coverageId", "displayName", "country", "mediaOutletCategory", "url"}
MEDIA_FIELDS = {"mediaId", "fileName", "mediaUrl", "mediaType", "source"}
ALLOWED_TONES = {"Factual", "Opinionated"}
ALLOWED_SENTIMENTS = {"Positive", "Neutral"}
ALLOWED_EVENT_TYPES = {"Facilitated", "Unfacilitated"}
REQUIRED_STRING_FIELDS = {
    "articleStatus",
    "articleTitle",
    "category",
    "contentDescription",
    "contentTitle",
    "documentTypeName",
    "eventType",
    "indexedDateTime",
    "lastUpdated",
    "lastUpdatedBy",
    "productType",
    "publishedDate",
    "tone",
    "toneSentiment",
    "uploadedBy",
    "vendorArticleId",
    "vendorIndexedTime",
}
NULLABLE_STRING_FIELDS = {"groupTitle", "newsType", "topic"}
REQUIRED_ARRAY_FIELDS = {
    "listOfCoverageBroadcast",
    "listOfCoverageOnline",
    "listOfCoveragePrints",
    "listOfMedia",
    "listOfSentiment",
    "listOfTags",
    "listOfUserGroupId",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_hash(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def mysql_tsv(value: Any) -> str:
    """Encode one value for LOAD DATA with tab delimiters and backslash escapes."""
    if value is None:
        return r"\N"
    if isinstance(value, bool):
        text = "1" if value else "0"
    else:
        text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("\0", "\\0")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\x1a", "\\Z")
    )


def write_tsv_row(handle: Any, values: Iterable[Any]) -> None:
    handle.write("\t".join(mysql_tsv(value) for value in values))
    handle.write("\n")


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def mysql_datetime(value: Any, field: str, article_id: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"article {article_id}: {field} must be a string or null")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"article {article_id}: invalid {field} {value!r}") from exc
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def require_integer(value: Any, field: str, article_id: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"article {article_id}: {field} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"article {article_id}: {field} outside {minimum}..{maximum}")
    return value


def relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def issue(
    issues: list[dict[str, Any]],
    staging_article_id: int,
    source_article_id: int,
    rule_code: str,
    severity: str,
    field_name: str,
    observed_value: Any,
    details: str,
) -> None:
    issues.append(
        {
            "staging_article_id": staging_article_id,
            "source_article_id": source_article_id,
            "rule_code": rule_code,
            "severity": severity,
            "field_name": field_name,
            "observed_value": None if observed_value is None else str(observed_value),
            "details": details,
        }
    )


def status_for(issues: list[dict[str, Any]]) -> str:
    if any(item["severity"] == "error" for item in issues):
        return "quarantined"
    if issues:
        return "review"
    return "ready"


def load_phase1_hashes(path: Path) -> dict[str, str]:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("phase") != 1 or manifest.get("status") != "passed":
        raise ValueError("Phase 1 manifest must be a passed phase-1 manifest")
    return {item["path"]: item["sha256"] for item in manifest["feeds"]}


def validate_mapping_contract(path: Path) -> str:
    contract = json.loads(path.read_text(encoding="utf-8"))
    mapped = {item["sourceField"] for item in contract.get("topLevelMappings", [])}
    if mapped != EXPECTED_TOP_LEVEL_FIELDS:
        missing = sorted(EXPECTED_TOP_LEVEL_FIELDS - mapped)
        unexpected = sorted(mapped - EXPECTED_TOP_LEVEL_FIELDS)
        raise ValueError(f"mapping contract mismatch; missing={missing}, unexpected={unexpected}")
    if contract.get("targetDatabase") != TARGET_DATABASE:
        raise ValueError(f"mapping contract target must be {TARGET_DATABASE}")
    return sha256_file(path)


def render_load_sql(
    bundle_dir: Path,
    batch: dict[str, Any],
    files: dict[str, str],
) -> str:
    table_loads = [
        (
            "UAT_stg_source_files",
            files["source_files"],
            "batch_id,source_file_id,source_path,file_name,sha256,byte_size,record_count,first_published_date,last_published_date",
        ),
        (
            "UAT_stg_articles",
            files["articles"],
            "batch_id,staging_article_id,source_file_id,source_row_number,source_article_id,document_id,vendor_article_id,article_title,content_title,content_description,topic,category,tone,tone_sentiment,event_type,document_type_id,document_type_name,product_type,article_status,group_title,news_type,published_date,vendor_indexed_time,indexed_date_time,last_updated,uploaded_by,last_updated_by,article_hero_image,sentiment_list,raw_json,record_hash,validation_status",
        ),
        (
            "UAT_stg_article_coverage",
            files["coverage"],
            "batch_id,staging_article_id,source_article_id,source_ordinal,coverage_type,coverage_id,display_name,country,media_outlet_category,url,record_hash",
        ),
        (
            "UAT_stg_article_media",
            files["media"],
            "batch_id,staging_article_id,source_article_id,source_ordinal,media_id,file_name,media_url,media_type,source,record_hash",
        ),
        (
            "UAT_stg_article_tags",
            files["tags"],
            "batch_id,staging_article_id,source_article_id,source_ordinal,tag,record_hash",
        ),
        (
            "UAT_stg_article_user_groups",
            files["user_groups"],
            "batch_id,staging_article_id,source_article_id,source_ordinal,user_group_id,record_hash",
        ),
        (
            "UAT_stg_quarantine",
            files["issues"],
            "batch_id,staging_article_id,source_article_id,rule_code,severity,field_name,observed_value,details",
        ),
    ]

    lines = [
        "-- Generated Phase 3 load script. Writes only UAT_stg_* tables.",
        "SET NAMES utf8mb4;",
        "SET SESSION sql_mode = 'STRICT_ALL_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';",
        "SET autocommit = 0;",
        "START TRANSACTION;",
        "INSERT INTO `UAT_stg_import_batches` (",
        "  batch_id,source_set_hash,source_set_name,mapping_contract_hash,loader_version,status,",
        "  expected_file_count,expected_article_count,expected_coverage_count,expected_media_count,",
        "  expected_tag_count,expected_user_group_count,expected_precomputed_issue_count,started_at,notes",
        ") VALUES (",
        "  "
        + ",".join(
            [
                sql_string(batch["batchId"]),
                sql_string(batch["sourceSetHash"]),
                sql_string(batch["sourceSetName"]),
                sql_string(batch["mappingContractHash"]),
                sql_string(LOADER_VERSION),
                "'loading'",
                str(batch["counts"]["files"]),
                str(batch["counts"]["articles"]),
                str(batch["counts"]["coverage"]),
                str(batch["counts"]["media"]),
                str(batch["counts"]["tags"]),
                str(batch["counts"]["userGroups"]),
                str(batch["counts"]["precomputedIssues"]),
                sql_string(batch["preparedAtMysql"]),
                sql_string("Phase 3 raw feed staging; canonical UAT tables are out of scope"),
            ]
        ),
        ");",
    ]

    for table, filename, columns in table_loads:
        absolute = (bundle_dir / filename).resolve().as_posix()
        lines.extend(
            [
                f"LOAD DATA LOCAL INFILE {sql_string(absolute)}",
                f"INTO TABLE `{table}`",
                "CHARACTER SET utf8mb4",
                "FIELDS TERMINATED BY '\\t' ESCAPED BY '\\\\'",
                "LINES TERMINATED BY '\\n'",
                f"({columns});",
            ]
        )

    batch_id = sql_string(batch["batchId"])
    lines.extend(
        [
            "INSERT INTO `UAT_stg_quarantine` (batch_id,staging_article_id,source_article_id,rule_code,severity,field_name,observed_value,details)",
            "SELECT s.batch_id,s.staging_article_id,s.source_article_id,'PRIMARY_KEY_COLLISION','error','articleId',",
            "       CAST(s.source_article_id AS CHAR),",
            "       CONCAT('Raw title: ',LEFT(COALESCE(s.article_title,''),500),' | Occupied UAT title: ',LEFT(COALESCE(u.article_title,''),500))",
            "FROM `UAT_stg_articles` s",
            "JOIN `UAT_articles` u ON u.article_id=s.source_article_id",
            f"WHERE s.batch_id={batch_id};",
            "UPDATE `UAT_stg_articles` s",
            "JOIN (SELECT DISTINCT batch_id,staging_article_id FROM `UAT_stg_quarantine` WHERE severity='error') q",
            "  ON q.batch_id=s.batch_id AND q.staging_article_id=s.staging_article_id",
            "SET s.validation_status='quarantined'",
            f"WHERE s.batch_id={batch_id};",
            "UPDATE `UAT_stg_articles` s",
            "JOIN (SELECT DISTINCT batch_id,staging_article_id FROM `UAT_stg_quarantine` WHERE severity='warning') q",
            "  ON q.batch_id=s.batch_id AND q.staging_article_id=s.staging_article_id",
            "SET s.validation_status='review'",
            f"WHERE s.batch_id={batch_id} AND s.validation_status='ready';",
            "UPDATE `UAT_stg_import_batches` b SET",
            "  actual_file_count=(SELECT COUNT(*) FROM `UAT_stg_source_files` f WHERE f.batch_id=b.batch_id),",
            "  actual_article_count=(SELECT COUNT(*) FROM `UAT_stg_articles` a WHERE a.batch_id=b.batch_id),",
            "  actual_coverage_count=(SELECT COUNT(*) FROM `UAT_stg_article_coverage` c WHERE c.batch_id=b.batch_id),",
            "  actual_media_count=(SELECT COUNT(*) FROM `UAT_stg_article_media` m WHERE m.batch_id=b.batch_id),",
            "  actual_tag_count=(SELECT COUNT(*) FROM `UAT_stg_article_tags` t WHERE t.batch_id=b.batch_id),",
            "  actual_user_group_count=(SELECT COUNT(*) FROM `UAT_stg_article_user_groups` g WHERE g.batch_id=b.batch_id),",
            "  actual_issue_count=(SELECT COUNT(*) FROM `UAT_stg_quarantine` q WHERE q.batch_id=b.batch_id),",
            "  status='loaded',loaded_at=NOW(6)",
            f"WHERE b.batch_id={batch_id};",
            "COMMIT;",
        ]
    )
    return "\n".join(lines) + "\n"


def render_validation_sql(batch_id: str) -> str:
    bid = sql_string(batch_id)
    return f"""-- Generated Phase 3 staging validation queries.
SELECT * FROM UAT_stg_import_batches WHERE batch_id={bid};
SELECT 'source_files' object_name,COUNT(*) row_count FROM UAT_stg_source_files WHERE batch_id={bid}
UNION ALL SELECT 'articles',COUNT(*) FROM UAT_stg_articles WHERE batch_id={bid}
UNION ALL SELECT 'coverage',COUNT(*) FROM UAT_stg_article_coverage WHERE batch_id={bid}
UNION ALL SELECT 'media',COUNT(*) FROM UAT_stg_article_media WHERE batch_id={bid}
UNION ALL SELECT 'tags',COUNT(*) FROM UAT_stg_article_tags WHERE batch_id={bid}
UNION ALL SELECT 'user_groups',COUNT(*) FROM UAT_stg_article_user_groups WHERE batch_id={bid}
UNION ALL SELECT 'issues',COUNT(*) FROM UAT_stg_quarantine WHERE batch_id={bid};
SELECT validation_status,COUNT(*) row_count FROM UAT_stg_articles WHERE batch_id={bid} GROUP BY validation_status ORDER BY validation_status;
SELECT rule_code,severity,COUNT(*) issue_count,COUNT(DISTINCT staging_article_id) article_count
FROM UAT_stg_quarantine WHERE batch_id={bid} GROUP BY rule_code,severity ORDER BY severity DESC,rule_code;
SELECT COUNT(*) duplicate_source_article_ids FROM (
  SELECT source_article_id FROM UAT_stg_articles WHERE batch_id={bid} GROUP BY source_article_id HAVING COUNT(*)>1
) d;
SELECT COUNT(*) duplicate_source_rows FROM (
  SELECT source_file_id,source_row_number FROM UAT_stg_articles WHERE batch_id={bid}
  GROUP BY source_file_id,source_row_number HAVING COUNT(*)>1
) d;
SELECT 'coverage' child_table,COUNT(*) orphan_count FROM UAT_stg_article_coverage c
LEFT JOIN UAT_stg_articles a ON a.batch_id=c.batch_id AND a.staging_article_id=c.staging_article_id
WHERE c.batch_id={bid} AND a.staging_article_id IS NULL
UNION ALL SELECT 'media',COUNT(*) FROM UAT_stg_article_media c
LEFT JOIN UAT_stg_articles a ON a.batch_id=c.batch_id AND a.staging_article_id=c.staging_article_id
WHERE c.batch_id={bid} AND a.staging_article_id IS NULL
UNION ALL SELECT 'tags',COUNT(*) FROM UAT_stg_article_tags c
LEFT JOIN UAT_stg_articles a ON a.batch_id=c.batch_id AND a.staging_article_id=c.staging_article_id
WHERE c.batch_id={bid} AND a.staging_article_id IS NULL
UNION ALL SELECT 'user_groups',COUNT(*) FROM UAT_stg_article_user_groups c
LEFT JOIN UAT_stg_articles a ON a.batch_id=c.batch_id AND a.staging_article_id=c.staging_article_id
WHERE c.batch_id={bid} AND a.staging_article_id IS NULL;
SELECT COUNT(*) source_file_hash_mismatches FROM UAT_stg_source_files f
WHERE f.batch_id={bid} AND NOT EXISTS (
  SELECT 1 FROM UAT_stg_articles a WHERE a.batch_id=f.batch_id AND a.source_file_id=f.source_file_id
);
"""


def prepare(args: argparse.Namespace) -> int:
    phase1_manifest = Path(args.phase1_manifest).resolve()
    mapping_contract = Path(args.mapping_contract).resolve()
    expected_hashes = load_phase1_hashes(phase1_manifest)
    mapping_hash = validate_mapping_contract(mapping_contract)

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError(f"output directory must be empty: {output_dir}")

    loaded_files: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    seen_article_ids: set[int] = set()

    for supplied in args.input:
        path = Path(supplied).resolve()
        rel = relative_path(path)
        if rel not in expected_hashes:
            raise ValueError(f"{rel} is not present in the passed Phase 1 manifest")
        current_hash = sha256_file(path)
        if current_hash != expected_hashes[rel]:
            raise ValueError(f"source hash changed for {rel}")
        records = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError(f"{rel} must contain a top-level JSON array")
        if not records:
            raise ValueError(f"{rel} is empty")
        first_date = min(str(record["publishedDate"]) for record in records)
        last_date = max(str(record["publishedDate"]) for record in records)
        loaded_files.append(
            {
                "path": path,
                "relativePath": rel,
                "fileName": path.name,
                "sha256": current_hash,
                "bytes": path.stat().st_size,
                "records": records,
                "recordCount": len(records),
                "firstPublishedDate": first_date,
                "lastPublishedDate": last_date,
            }
        )

    loaded_files.sort(key=lambda item: item["firstPublishedDate"])

    for file_info in loaded_files:
        for row_number, record in enumerate(file_info["records"], start=1):
            if not isinstance(record, dict):
                raise ValueError(f"{file_info['relativePath']} row {row_number}: expected object")
            keys = set(record)
            if keys != EXPECTED_TOP_LEVEL_FIELDS:
                missing = sorted(EXPECTED_TOP_LEVEL_FIELDS - keys)
                unexpected = sorted(keys - EXPECTED_TOP_LEVEL_FIELDS)
                raise ValueError(
                    f"{file_info['relativePath']} row {row_number}: schema drift; "
                    f"missing={missing}, unexpected={unexpected}"
                )
            article_id = require_integer(record["articleId"], "articleId", -1, -2147483648, 2147483647)
            if article_id in seen_article_ids:
                raise ValueError(f"duplicate source articleId {article_id}")
            seen_article_ids.add(article_id)
            all_records.append(
                {
                    "file": file_info,
                    "sourceRowNumber": row_number,
                    "record": record,
                }
            )

    vendor_counts = Counter(item["record"]["vendorArticleId"] for item in all_records)
    source_set_material = "\n".join(
        f"{item['relativePath']}\0{item['sha256']}\0{item['bytes']}\0{item['recordCount']}"
        for item in loaded_files
    )
    source_set_hash = sha256_bytes(source_set_material.encode("utf-8"))
    batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ai-animation:{source_set_hash}"))
    prepared_at = datetime.now(timezone.utc)

    names = {
        "source_files": "source_files.tsv",
        "articles": "articles.tsv",
        "coverage": "article_coverage.tsv",
        "media": "article_media.tsv",
        "tags": "article_tags.tsv",
        "user_groups": "article_user_groups.tsv",
        "issues": "quarantine.tsv",
    }
    handles = {
        key: (output_dir / filename).open("w", encoding="utf-8", newline="\n")
        for key, filename in names.items()
    }

    counts = defaultdict(int)
    all_issues: list[dict[str, Any]] = []
    file_ids: dict[str, int] = {}
    staging_id = 0

    try:
        for source_file_id, file_info in enumerate(loaded_files, start=1):
            file_ids[file_info["relativePath"]] = source_file_id
            write_tsv_row(
                handles["source_files"],
                [
                    batch_id,
                    source_file_id,
                    file_info["relativePath"],
                    file_info["fileName"],
                    file_info["sha256"],
                    file_info["bytes"],
                    file_info["recordCount"],
                    file_info["firstPublishedDate"].replace("T", " "),
                    file_info["lastPublishedDate"].replace("T", " "),
                ],
            )
            counts["files"] += 1

        for source_item in all_records:
            staging_id += 1
            file_info = source_item["file"]
            source_file_id = file_ids[file_info["relativePath"]]
            source_row_number = source_item["sourceRowNumber"]
            record = source_item["record"]
            article_id = record["articleId"]

            require_integer(record["documentId"], "documentId", article_id, -2147483648, 2147483647)
            require_integer(record["documentTypeId"], "documentTypeId", article_id, 0, 255)
            for field in REQUIRED_STRING_FIELDS:
                if not isinstance(record[field], str):
                    raise ValueError(f"article {article_id}: {field} must be a string")
            for field in NULLABLE_STRING_FIELDS:
                if record[field] is not None and not isinstance(record[field], str):
                    raise ValueError(f"article {article_id}: {field} must be a string or null")
            for field in REQUIRED_ARRAY_FIELDS:
                if not isinstance(record[field], list):
                    raise ValueError(f"article {article_id}: {field} must be an array")
            if record["tone"] not in ALLOWED_TONES:
                raise ValueError(f"article {article_id}: unexpected tone {record['tone']!r}")
            if record["eventType"] not in ALLOWED_EVENT_TYPES:
                raise ValueError(f"article {article_id}: unexpected eventType {record['eventType']!r}")
            if not isinstance(record["articleHeroImage"], dict):
                raise ValueError(f"article {article_id}: articleHeroImage must be an object")
            if record["articleHeroImage"]:
                raise ValueError(f"article {article_id}: populated articleHeroImage is schema drift")
            if not isinstance(record["listOfSentiment"], list):
                raise ValueError(f"article {article_id}: listOfSentiment must be an array")
            if record["listOfSentiment"]:
                raise ValueError(f"article {article_id}: populated listOfSentiment is schema drift")

            record_issues: list[dict[str, Any]] = []
            if record["topic"] in (None, ""):
                issue(record_issues, staging_id, article_id, "MISSING_TOPIC", "warning", "topic", record["topic"], "Topic is absent; preserve NULL and do not infer.")
            if record["category"] == "":
                issue(record_issues, staging_id, article_id, "EMPTY_CATEGORY", "error", "category", "", "Category is an empty string and requires approval before canonical load.")
            if record["toneSentiment"] not in ALLOWED_SENTIMENTS:
                issue(record_issues, staging_id, article_id, "NONCONFORMING_SENTIMENT", "error", "toneSentiment", record["toneSentiment"], "Current UAT semantic domain permits Positive or Neutral only.")
            if not record["listOfTags"]:
                issue(record_issues, staging_id, article_id, "ZERO_TAGS", "warning", "listOfTags", "[]", "Article has no source tags; do not invent tags.")
            for field, limit in (("vendorArticleId", 400), ("articleTitle", 500), ("contentTitle", 500)):
                value = record[field]
                if value is not None and len(value) > limit:
                    issue(record_issues, staging_id, article_id, "OVERLENGTH_FIELD", "error", field, len(value), f"Source length {len(value)} exceeds target limit {limit}; truncation is prohibited.")
            vendor_id = record["vendorArticleId"]
            if vendor_id is not None and vendor_counts[vendor_id] > 1:
                issue(record_issues, staging_id, article_id, "DUPLICATE_VENDOR_ARTICLE_ID", "warning", "vendorArticleId", vendor_id, f"vendorArticleId occurs in {vendor_counts[vendor_id]} source records; do not merge automatically.")

            for tag in record["listOfTags"]:
                if not isinstance(tag, str) or not tag or len(tag) > 200:
                    raise ValueError(f"article {article_id}: invalid tag {tag!r}")
            for user_group in record["listOfUserGroupId"]:
                require_integer(user_group, "userGroupId", article_id, -2147483648, 2147483647)

            write_tsv_row(
                handles["articles"],
                [
                    batch_id,
                    staging_id,
                    source_file_id,
                    source_row_number,
                    article_id,
                    record["documentId"],
                    record["vendorArticleId"],
                    record["articleTitle"],
                    record["contentTitle"],
                    record["contentDescription"],
                    record["topic"],
                    record["category"],
                    record["tone"],
                    record["toneSentiment"],
                    record["eventType"],
                    record["documentTypeId"],
                    record["documentTypeName"],
                    record["productType"],
                    record["articleStatus"],
                    record["groupTitle"],
                    record["newsType"],
                    mysql_datetime(record["publishedDate"], "publishedDate", article_id),
                    mysql_datetime(record["vendorIndexedTime"], "vendorIndexedTime", article_id),
                    mysql_datetime(record["indexedDateTime"], "indexedDateTime", article_id),
                    mysql_datetime(record["lastUpdated"], "lastUpdated", article_id),
                    record["uploadedBy"],
                    record["lastUpdatedBy"],
                    canonical_json(record["articleHeroImage"]),
                    canonical_json(record["listOfSentiment"]),
                    canonical_json(record),
                    record_hash(record),
                    status_for(record_issues),
                ],
            )
            counts["articles"] += 1

            coverage_groups = [
                ("broadcast", record["listOfCoverageBroadcast"]),
                ("online", record["listOfCoverageOnline"]),
                ("print", record["listOfCoveragePrints"]),
            ]
            for coverage_type, coverage_items in coverage_groups:
                if not isinstance(coverage_items, list):
                    raise ValueError(f"article {article_id}: {coverage_type} coverage must be an array")
                for ordinal, coverage in enumerate(coverage_items, start=1):
                    if not isinstance(coverage, dict) or set(coverage) != COVERAGE_FIELDS:
                        raise ValueError(f"article {article_id}: coverage schema drift")
                    require_integer(coverage["coverageId"], "coverageId", article_id, -2147483648, 2147483647)
                    for field in ("displayName", "country", "mediaOutletCategory"):
                        if not isinstance(coverage[field], str):
                            raise ValueError(f"article {article_id}: coverage {field} must be a string")
                    if coverage["url"] is not None and not isinstance(coverage["url"], str):
                        raise ValueError(f"article {article_id}: coverage url must be a string or null")
                    child = {"sourceArticleId": article_id, "coverageType": coverage_type, "sourceOrdinal": ordinal, **coverage}
                    write_tsv_row(
                        handles["coverage"],
                        [batch_id, staging_id, article_id, ordinal, coverage_type, coverage["coverageId"], coverage["displayName"], coverage["country"], coverage["mediaOutletCategory"], coverage["url"], record_hash(child)],
                    )
                    counts["coverage"] += 1

            if not isinstance(record["listOfMedia"], list):
                raise ValueError(f"article {article_id}: listOfMedia must be an array")
            for ordinal, media in enumerate(record["listOfMedia"], start=1):
                if not isinstance(media, dict) or set(media) != MEDIA_FIELDS:
                    raise ValueError(f"article {article_id}: media schema drift")
                require_integer(media["mediaId"], "mediaId", article_id, -2147483648, 2147483647)
                for field in ("fileName", "mediaUrl", "mediaType", "source"):
                    if not isinstance(media[field], str):
                        raise ValueError(f"article {article_id}: media {field} must be a string")
                child = {"sourceArticleId": article_id, "sourceOrdinal": ordinal, **media}
                write_tsv_row(
                    handles["media"],
                    [batch_id, staging_id, article_id, ordinal, media["mediaId"], media["fileName"], media["mediaUrl"], media["mediaType"], media["source"], record_hash(child)],
                )
                counts["media"] += 1

            for ordinal, tag in enumerate(record["listOfTags"], start=1):
                child = {"sourceArticleId": article_id, "sourceOrdinal": ordinal, "tag": tag}
                write_tsv_row(handles["tags"], [batch_id, staging_id, article_id, ordinal, tag, record_hash(child)])
                counts["tags"] += 1

            for ordinal, user_group in enumerate(record["listOfUserGroupId"], start=1):
                child = {"sourceArticleId": article_id, "sourceOrdinal": ordinal, "userGroupId": user_group}
                write_tsv_row(handles["user_groups"], [batch_id, staging_id, article_id, ordinal, user_group, record_hash(child)])
                counts["userGroups"] += 1

            all_issues.extend(record_issues)

        for item in all_issues:
            write_tsv_row(
                handles["issues"],
                [batch_id, item["staging_article_id"], item["source_article_id"], item["rule_code"], item["severity"], item["field_name"], item["observed_value"], item["details"]],
            )
        counts["precomputedIssues"] = len(all_issues)
    finally:
        for handle in handles.values():
            handle.close()

    expected = {
        "files": 4,
        "articles": 8215,
        "coverage": 8215,
        "media": 347,
        "tags": 229414,
        "userGroups": 8215,
    }
    for name, wanted in expected.items():
        if counts[name] != wanted:
            raise ValueError(f"prepared {name} count {counts[name]} does not match Phase 1 count {wanted}")

    batch = {
        "bundleVersion": "phase3-staging-bundle.v1",
        "loaderVersion": LOADER_VERSION,
        "targetDatabase": TARGET_DATABASE,
        "sourceSetName": args.source_set_name,
        "sourceSetHash": source_set_hash,
        "batchId": batch_id,
        "mappingContractHash": mapping_hash,
        "preparedAt": prepared_at.isoformat(timespec="seconds"),
        "preparedAtMysql": prepared_at.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "counts": dict(counts),
        "sourceFiles": [
            {
                key: item[key]
                for key in ("relativePath", "fileName", "sha256", "bytes", "recordCount", "firstPublishedDate", "lastPublishedDate")
            }
            for item in loaded_files
        ],
    }

    load_sql = render_load_sql(output_dir, batch, names)
    validation_sql = render_validation_sql(batch_id)
    (output_dir / "phase3_load.sql").write_text(load_sql, encoding="utf-8")
    (output_dir / "phase3_validate.sql").write_text(validation_sql, encoding="utf-8")

    bundle_files = {}
    for path in sorted(output_dir.iterdir()):
        if path.name == "bundle_manifest.json":
            continue
        row_count = None
        if path.suffix == ".tsv":
            with path.open("r", encoding="utf-8") as handle:
                row_count = sum(1 for _ in handle)
        bundle_files[path.name] = {"bytes": path.stat().st_size, "sha256": sha256_file(path), "rows": row_count}
    batch["bundleFiles"] = bundle_files
    (output_dir / "bundle_manifest.json").write_text(json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"status": "prepared", "outputDir": str(output_dir), **batch}, ensure_ascii=False, indent=2))
    return 0


def verify_bundle(args: argparse.Namespace) -> int:
    bundle_dir = Path(args.bundle_dir).resolve()
    manifest_path = bundle_dir / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors: list[str] = []

    for filename, expected in manifest["bundleFiles"].items():
        path = bundle_dir / filename
        if not path.is_file():
            errors.append(f"missing bundle file: {filename}")
            continue
        actual_hash = sha256_file(path)
        if actual_hash != expected["sha256"]:
            errors.append(f"hash mismatch for {filename}: {actual_hash} != {expected['sha256']}")
        if expected["rows"] is not None:
            with path.open("r", encoding="utf-8") as handle:
                actual_rows = sum(1 for _ in handle)
            if actual_rows != expected["rows"]:
                errors.append(f"row mismatch for {filename}: {actual_rows} != {expected['rows']}")

    staging_to_file: dict[int, int] = {}
    signature_state: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)

    def add_signature(table: str, file_id: int, hash_value: str) -> None:
        state = signature_state[table].setdefault(file_id, {"count": 0, "digest": hashlib.sha256()})
        state["count"] += 1
        state["digest"].update(hash_value.encode("ascii"))

    with (bundle_dir / "articles.tsv").open("r", encoding="utf-8") as handle:
        for line in handle:
            columns = line.rstrip("\n").split("\t")
            staging_id = int(columns[1])
            file_id = int(columns[2])
            staging_to_file[staging_id] = file_id
            add_signature("articles", file_id, columns[30])

    child_specs = {
        "coverage": ("article_coverage.tsv", 10),
        "media": ("article_media.tsv", 9),
        "tags": ("article_tags.tsv", 5),
        "user_groups": ("article_user_groups.tsv", 5),
    }
    for table, (filename, hash_index) in child_specs.items():
        with (bundle_dir / filename).open("r", encoding="utf-8") as handle:
            for line in handle:
                columns = line.rstrip("\n").split("\t")
                staging_id = int(columns[1])
                add_signature(table, staging_to_file[staging_id], columns[hash_index])

    signatures = {
        table: {
            str(file_id): {"count": state["count"], "recordHashChain": state["digest"].hexdigest()}
            for file_id, state in sorted(files.items())
        }
        for table, files in sorted(signature_state.items())
    }
    result = {
        "verificationVersion": "phase3-bundle-verification.v1",
        "status": "passed" if not errors else "failed",
        "batchId": manifest["batchId"],
        "sourceSetHash": manifest["sourceSetHash"],
        "errors": errors,
        "recordHashSignatures": signatures,
    }
    if args.output:
        Path(args.output).resolve().write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare", help="validate feeds and create a deterministic staging bundle")
    prepare_parser.add_argument("--input", action="append", required=True, help="source feed JSON file; repeat four times")
    prepare_parser.add_argument("--phase1-manifest", required=True)
    prepare_parser.add_argument("--mapping-contract", required=True)
    prepare_parser.add_argument("--output-dir", required=True)
    prepare_parser.add_argument("--source-set-name", default="Animation Technology feed exports 2026-04 through 2026-07")
    prepare_parser.set_defaults(func=prepare)
    verify_parser = subparsers.add_parser("verify-bundle", help="recompute bundle hashes, row counts and record-hash chains")
    verify_parser.add_argument("--bundle-dir", required=True)
    verify_parser.add_argument("--output")
    verify_parser.set_defaults(func=verify_bundle)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
