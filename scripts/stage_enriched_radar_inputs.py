#!/usr/bin/env python3
"""HISTORICAL ONLY: prepare the superseded enriched-input staging bundle.

Do not use this script for new database loads. Compiled Markdown is now the sole
source of truth and ``project_wiki_to_uat.py`` is the only active UAT projector.
This file remains available solely to reproduce older run artifacts.

Automatic admission is strict: tone, event type, metadata and tags must all be
auto-applicable, and neither model pass may have requested review. Other
articles remain in staging with explicit review reasons and are excluded from
the canonical UAT load. A reviewed approval JSON can admit them on a later run.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from enrich_radar_inputs import parse_frontmatter, split_note  # noqa: E402
from stage_mysql_feeds import render_load_sql, render_validation_sql  # noqa: E402


LOADER_VERSION = "enriched-radar-input-stager.v1"
MAPPING_CONTRACT = "enriched-markdown-to-uat.v1"
TARGET_DATABASE = "AI_Animation_UAT"
TONE_VALUES = {"Factual", "Opinionated"}
EVENT_VALUES = {"Facilitated", "Unfacilitated"}
APPROVAL_FIELDS = {
    "tone", "eventType", "tags", "outletName", "outletCountry", "category",
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
    if value is None:
        return r"\N"
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
    handle.write("\t".join(mysql_tsv(value) for value in values) + "\n")


def sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def mysql_datetime(value: Any) -> str:
    if value is None or str(value).strip() == "":
        raise ValueError("publishedDate is required")
    text = str(value).strip()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def relative_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def display_from_slug(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("-", " ")).strip().title()


def load_assessments(paths: list[Path]) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("assessments", []):
            article_id = str(item.get("articleId") or "").strip()
            if article_id:
                output[article_id] = item
    return output


def load_approvals(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("approvals", payload)
    if not isinstance(rows, dict):
        raise ValueError("approval file must be an object keyed by external article ID")
    output = {}
    for article_id, approval in rows.items():
        if not isinstance(approval, dict) or not approval.get("approved"):
            continue
        missing = sorted(APPROVAL_FIELDS - set(approval.get("fields", {})))
        if missing:
            raise ValueError(f"approval {article_id} lacks fields: {', '.join(missing)}")
        if not approval.get("approvedBy") or not approval.get("approvedAt"):
            raise ValueError(f"approval {article_id} requires approvedBy and approvedAt")
        output[str(article_id)] = approval
    return output


def assessment_state(
    article_id: str,
    metadata: dict[str, Any],
    assessment: dict[str, Any] | None,
    approval: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    values = {
        "tone": metadata.get("tone"),
        "eventType": metadata.get("eventType"),
        "tags": list(metadata.get("tags") or []),
        "outletName": "",
        "outletCountry": (metadata.get("countries") or [""])[0],
        "category": metadata.get("category") or "",
    }
    outlets = list(metadata.get("outlets") or [])
    if outlets:
        values["outletName"] = display_from_slug(str(outlets[0]))

    problems: list[dict[str, str]] = []
    if approval:
        values.update(approval["fields"])
    elif not assessment:
        problems.append({
            "rule": "MISSING_ENRICHMENT_ASSESSMENT",
            "severity": "error",
            "field": "assessment",
            "details": "No enrichment assessment was supplied for this article.",
        })
    else:
        consensus = assessment.get("consensus") or {}
        auto = consensus.get("autoApplicable") or {}
        if auto.get("metadata"):
            values["outletName"] = consensus.get("outletName") or values["outletName"]
            values["outletCountry"] = consensus.get("outletCountry") or values["outletCountry"]
            institutional = consensus.get("institutionalCategory")
            if institutional and institutional != "Non-institutional":
                values["category"] = institutional
        if auto.get("tags"):
            values["tags"] = list(consensus.get("issueTags") or values["tags"])
        if auto.get("tone"):
            values["tone"] = consensus.get("tone") or values["tone"]
        if auto.get("eventType"):
            values["eventType"] = consensus.get("eventType") or values["eventType"]

        for field in ("tone", "eventType", "metadata", "tags"):
            if not auto.get(field):
                problems.append({
                    "rule": "ENRICHMENT_NOT_AUTO_APPLICABLE",
                    "severity": "warning",
                    "field": field,
                    "details": f"{field} disagreed between passes or fell below the confidence threshold.",
                })
        if consensus.get("reviewRequired"):
            problems.append({
                "rule": "MODEL_REQUESTED_REVIEW",
                "severity": "warning",
                "field": "assessment",
                "details": "; ".join(consensus.get("reviewReasons") or ["A model pass requested review."]),
            })

    checks = [
        ("tags", bool(values["tags"]), "At least one approved issue tag is required."),
        ("outletName", bool(str(values["outletName"]).strip()), "An approved outlet name is required."),
        ("outletCountry", bool(str(values["outletCountry"]).strip()), "An approved outlet country is required."),
        ("category", bool(str(values["category"]).strip()), "A category is required."),
        ("tone", values["tone"] in TONE_VALUES, "Tone must be Factual or Opinionated."),
        ("eventType", values["eventType"] in EVENT_VALUES, "Event type must be Facilitated or Unfacilitated."),
    ]
    existing_fields = {item["field"] for item in problems}
    for field, valid, details in checks:
        if not valid and field not in existing_fields:
            problems.append({
                "rule": "MISSING_OR_INVALID_RADAR_INPUT",
                "severity": "warning",
                "field": field,
                "details": details,
            })
    return values, problems


def phase3_validation_sql(batch_id: str, counts: dict[str, int]) -> str:
    bid = sql_string(batch_id)
    base = render_validation_sql(batch_id)
    gates = [
        ("actual_file_count", counts["files"]),
        ("actual_article_count", counts["articles"]),
        ("actual_coverage_count", counts["coverage"]),
        ("actual_media_count", counts["media"]),
        ("actual_tag_count", counts["tags"]),
        ("actual_user_group_count", counts["userGroups"]),
        ("actual_issue_count", counts["precomputedIssues"]),
    ]
    predicates = "\n  AND ".join(f"`{field}`={value}" for field, value in gates)
    return base + f"""
UPDATE `UAT_stg_import_batches`
SET `status`='validated',`validated_at`=UTC_TIMESTAMP(6)
WHERE `batch_id`={bid}
  AND `status`='loaded'
  AND {predicates};
SELECT `batch_id`,`status`,`actual_article_count`,`actual_issue_count`
FROM `UAT_stg_import_batches` WHERE `batch_id`={bid};
"""


def phase4_transform_sql(
    batch_id: str,
    transform_id: str,
) -> str:
    bid, tid = sql_string(batch_id), sql_string(transform_id)
    return f"""-- Generated enriched-input transformation. UAT candidate tables only.
SET NAMES utf8mb4;
SET SESSION sql_mode='STRICT_ALL_TABLES,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';
USE `{TARGET_DATABASE}`;
SET @source_batch_id={bid};
SET @transform_id={tid};
SET @allocation_ceiling=(SELECT COALESCE(MAX(`article_id`),0) FROM `UAT_articles`);
START TRANSACTION;

INSERT INTO `UAT_stg_quarantine`
(`batch_id`,`staging_article_id`,`source_article_id`,`rule_code`,`severity`,`field_name`,`observed_value`,`details`)
SELECT a.`batch_id`,a.`staging_article_id`,a.`source_article_id`,
       'DUPLICATE_EXTERNAL_ID_IN_UAT','warning','vendorArticleId',a.`vendor_article_id`,
       'The external article ID already exists in canonical UAT; review rather than duplicate.'
FROM `UAT_stg_articles` a
JOIN `UAT_articles` u ON u.`vendor_article_id`=a.`vendor_article_id`
WHERE a.`batch_id`=@source_batch_id
ON DUPLICATE KEY UPDATE `details`=VALUES(`details`);

INSERT INTO `UAT_p4_transform_batches`
(`transform_id`,`source_batch_id`,`transform_version`,`status`,`allocation_ceiling`,
 `source_article_count`,`started_at`,`notes`)
SELECT @transform_id,@source_batch_id,'enriched-input-transform.v1','building',
       @allocation_ceiling,b.`actual_article_count`,UTC_TIMESTAMP(6),
       'External IDs preserved in vendor_article_id; positive UAT IDs allocated above current maximum.'
FROM `UAT_stg_import_batches` b
WHERE b.`batch_id`=@source_batch_id AND b.`status`='validated';

INSERT INTO `UAT_p4_article_identity`
(`transform_id`,`source_batch_id`,`staging_article_id`,`source_article_id`,`target_article_id`,
 `identity_action`,`disposition`,`blocking_issue_count`,`warning_issue_count`,`source_record_hash`)
WITH issue_counts AS (
  SELECT q.`batch_id`,q.`staging_article_id`,
         SUM(q.`severity`='error' AND q.`review_status` IN ('pending','rejected')) blocking_count,
         SUM(q.`severity`='warning' AND q.`review_status`='pending') warning_count
  FROM `UAT_stg_quarantine` q
  WHERE q.`batch_id`=@source_batch_id
  GROUP BY q.`batch_id`,q.`staging_article_id`
)
SELECT @transform_id,a.`batch_id`,a.`staging_article_id`,a.`source_article_id`,
       @allocation_ceiling+ROW_NUMBER() OVER (ORDER BY a.`staging_article_id`),
       'remapped_collision',
       CASE WHEN COALESCE(i.blocking_count,0)>0 THEN 'quarantined'
            WHEN COALESCE(i.warning_count,0)>0 THEN 'review' ELSE 'candidate' END,
       COALESCE(i.blocking_count,0),COALESCE(i.warning_count,0),a.`record_hash`
FROM `UAT_stg_articles` a
LEFT JOIN issue_counts i ON i.`batch_id`=a.`batch_id`
 AND i.`staging_article_id`=a.`staging_article_id`
WHERE a.`batch_id`=@source_batch_id;

INSERT INTO `UAT_p4_articles`
(`transform_id`,`source_batch_id`,`staging_article_id`,`source_article_id`,`article_id`,
 `document_id`,`vendor_article_id`,`article_title`,`content_title`,`content_description`,
 `topic`,`category`,`tone`,`tone_sentiment`,`event_type`,`document_type_id`,
 `document_type_name`,`product_type`,`article_status`,`group_title`,`news_type`,
 `published_date`,`vendor_indexed_time`,`indexed_date_time`,`last_updated`,
 `uploaded_by`,`last_updated_by`,`source_record_hash`)
SELECT i.`transform_id`,a.`batch_id`,a.`staging_article_id`,a.`source_article_id`,
       i.`target_article_id`,i.`target_article_id`,a.`vendor_article_id`,a.`article_title`,
       a.`content_title`,a.`content_description`,a.`topic`,a.`category`,a.`tone`,
       a.`tone_sentiment`,a.`event_type`,a.`document_type_id`,a.`document_type_name`,
       a.`product_type`,a.`article_status`,a.`group_title`,a.`news_type`,a.`published_date`,
       a.`vendor_indexed_time`,a.`indexed_date_time`,a.`last_updated`,a.`uploaded_by`,
       a.`last_updated_by`,a.`record_hash`
FROM `UAT_stg_articles` a
JOIN `UAT_p4_article_identity` i ON i.`source_batch_id`=a.`batch_id`
 AND i.`staging_article_id`=a.`staging_article_id`
WHERE i.`transform_id`=@transform_id AND i.`disposition`='candidate';

INSERT INTO `UAT_p4_article_coverage`
(`transform_id`,`article_id`,`source_ordinal`,`coverage_id`,`coverage_type`,
 `display_name`,`country`,`media_outlet_category`,`url`,`source_record_hash`)
SELECT p.`transform_id`,p.`article_id`,c.`source_ordinal`,c.`coverage_id`,c.`coverage_type`,
       c.`display_name`,c.`country`,c.`media_outlet_category`,c.`url`,c.`record_hash`
FROM `UAT_p4_articles` p
JOIN `UAT_stg_article_coverage` c ON c.`batch_id`=p.`source_batch_id`
 AND c.`staging_article_id`=p.`staging_article_id`
WHERE p.`transform_id`=@transform_id;

INSERT INTO `UAT_p4_article_tags`
(`transform_id`,`article_id`,`source_ordinal`,`tag`,`source_record_hash`)
SELECT p.`transform_id`,p.`article_id`,t.`source_ordinal`,t.`tag`,t.`record_hash`
FROM `UAT_p4_articles` p
JOIN `UAT_stg_article_tags` t ON t.`batch_id`=p.`source_batch_id`
 AND t.`staging_article_id`=p.`staging_article_id`
WHERE p.`transform_id`=@transform_id;

INSERT INTO `UAT_p4_holds`
(`transform_id`,`source_batch_id`,`staging_article_id`,`source_article_id`,
 `target_article_id`,`disposition`,`blocking_issue_count`,`warning_issue_count`,
 `rule_codes`,`source_record_hash`)
SELECT i.`transform_id`,i.`source_batch_id`,i.`staging_article_id`,i.`source_article_id`,
       i.`target_article_id`,i.`disposition`,i.`blocking_issue_count`,i.`warning_issue_count`,
       GROUP_CONCAT(DISTINCT q.`rule_code` ORDER BY q.`rule_code` SEPARATOR ','),
       i.`source_record_hash`
FROM `UAT_p4_article_identity` i
JOIN `UAT_stg_quarantine` q ON q.`batch_id`=i.`source_batch_id`
 AND q.`staging_article_id`=i.`staging_article_id`
WHERE i.`transform_id`=@transform_id AND i.`disposition` IN ('review','quarantined')
GROUP BY i.`transform_id`,i.`source_batch_id`,i.`staging_article_id`,i.`source_article_id`,
 i.`target_article_id`,i.`disposition`,i.`blocking_issue_count`,i.`warning_issue_count`,
 i.`source_record_hash`;

UPDATE `UAT_p4_transform_batches`
SET `status`='built',
 `candidate_article_count`=(SELECT COUNT(*) FROM `UAT_p4_articles` WHERE `transform_id`=@transform_id),
 `review_article_count`=(SELECT COUNT(*) FROM `UAT_p4_article_identity` WHERE `transform_id`=@transform_id AND `disposition`='review'),
 `quarantined_article_count`=(SELECT COUNT(*) FROM `UAT_p4_article_identity` WHERE `transform_id`=@transform_id AND `disposition`='quarantined'),
 `candidate_coverage_count`=(SELECT COUNT(*) FROM `UAT_p4_article_coverage` WHERE `transform_id`=@transform_id),
 `candidate_media_count`=0,
 `candidate_tag_count`=(SELECT COUNT(*) FROM `UAT_p4_article_tags` WHERE `transform_id`=@transform_id),
 `candidate_user_group_count`=0,
 `built_at`=UTC_TIMESTAMP(6)
WHERE `transform_id`=@transform_id;
COMMIT;
SELECT @transform_id AS transform_id;
"""


def phase4_validation_sql(
    transform_id: str,
    expected: dict[str, int],
) -> str:
    tid = sql_string(transform_id)
    return f"""-- Generated Phase 4 gate. Status changes only when every count matches.
USE `{TARGET_DATABASE}`;
UPDATE `UAT_p4_transform_batches` t
SET t.`status`='validated',t.`validated_at`=UTC_TIMESTAMP(6)
WHERE t.`transform_id`={tid} AND t.`status`='built'
  AND t.`candidate_article_count`={expected['readyArticles']}
  AND t.`review_article_count`={expected['reviewArticles']}
  AND t.`quarantined_article_count`={expected['quarantinedArticles']}
  AND t.`candidate_coverage_count`={expected['readyCoverage']}
  AND t.`candidate_tag_count`={expected['readyTags']}
  AND NOT EXISTS (
    SELECT 1 FROM `UAT_p4_article_tags` x
    LEFT JOIN `UAT_p4_articles` a ON a.`transform_id`=x.`transform_id`
     AND a.`article_id`=x.`article_id`
    WHERE x.`transform_id`={tid} AND a.`article_id` IS NULL
  )
  AND NOT EXISTS (
    SELECT 1 FROM `UAT_p4_article_coverage` x
    LEFT JOIN `UAT_p4_articles` a ON a.`transform_id`=x.`transform_id`
     AND a.`article_id`=x.`article_id`
    WHERE x.`transform_id`={tid} AND a.`article_id` IS NULL
  );
SELECT * FROM `UAT_p4_transform_batches` WHERE `transform_id`={tid};
SELECT `disposition`,COUNT(*) FROM `UAT_p4_article_identity`
WHERE `transform_id`={tid} GROUP BY `disposition`;
"""


def phase5_transaction_sql(transform_id: str, load_id: str) -> str:
    tid, lid = sql_string(transform_id), sql_string(load_id)
    procedure = "load_enriched_radar_" + re.sub(r"[^a-z0-9]", "", load_id.lower())[:16]
    return f"""-- Generated atomic canonical-UAT load. Any failed gate rolls back all article data.
USE `{TARGET_DATABASE}`;
DROP PROCEDURE IF EXISTS `{procedure}`;
DELIMITER //
CREATE PROCEDURE `{procedure}`()
BEGIN
  DECLARE expected_articles,expected_coverage,expected_tags INT DEFAULT NULL;
  DECLARE before_articles,before_coverage,before_tags INT DEFAULT 0;
  DECLARE inserted_articles,inserted_coverage,inserted_tags INT DEFAULT 0;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  SELECT `candidate_article_count`,`candidate_coverage_count`,`candidate_tag_count`
    INTO expected_articles,expected_coverage,expected_tags
  FROM `UAT_p4_transform_batches`
  WHERE `transform_id`={tid} AND `status`='validated';
  IF expected_articles IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Phase 4 transform is not validated';
  END IF;
  SELECT COUNT(*) INTO before_articles FROM `UAT_articles`;
  SELECT COUNT(*) INTO before_coverage FROM `UAT_article_coverage`;
  SELECT COUNT(*) INTO before_tags FROM `UAT_article_tags`;

  START TRANSACTION;
  INSERT INTO `UAT_p5_load_batches`
  (`load_id`,`transform_id`,`load_version`,`status`,`expected_article_count`,
   `expected_coverage_count`,`expected_media_count`,`expected_tag_count`,
   `expected_user_group_count`,`before_article_count`,`before_coverage_count`,
   `before_media_count`,`before_tag_count`,`before_user_group_count`,`started_at`,`notes`)
  VALUES
  ({lid},{tid},'enriched-input-uat-load.v1','in_transaction',expected_articles,
   expected_coverage,0,expected_tags,0,before_articles,before_coverage,
   (SELECT COUNT(*) FROM `UAT_article_media`),before_tags,
   (SELECT COUNT(*) FROM `UAT_article_user_groups`),UTC_TIMESTAMP(6),
   'Strictly reviewed enriched-input load; production excluded.');

  INSERT INTO `UAT_articles`
  (`article_id`,`document_id`,`vendor_article_id`,`article_title`,`content_title`,
   `content_description`,`topic`,`category`,`tone`,`tone_sentiment`,`event_type`,
   `document_type_id`,`document_type_name`,`product_type`,`article_status`,
   `group_title`,`news_type`,`published_date`,`vendor_indexed_time`,
   `indexed_date_time`,`last_updated`,`uploaded_by`,`last_updated_by`)
  SELECT `article_id`,`document_id`,`vendor_article_id`,`article_title`,`content_title`,
   `content_description`,`topic`,`category`,`tone`,`tone_sentiment`,`event_type`,
   `document_type_id`,`document_type_name`,`product_type`,`article_status`,
   `group_title`,`news_type`,`published_date`,`vendor_indexed_time`,
   `indexed_date_time`,`last_updated`,`uploaded_by`,`last_updated_by`
  FROM `UAT_p4_articles` WHERE `transform_id`={tid} ORDER BY `article_id`;
  SET inserted_articles=ROW_COUNT();

  INSERT INTO `UAT_article_coverage`
  (`article_id`,`coverage_id`,`coverage_type`,`display_name`,`country`,
   `media_outlet_category`,`url`)
  SELECT `article_id`,`coverage_id`,`coverage_type`,`display_name`,`country`,
   `media_outlet_category`,`url`
  FROM `UAT_p4_article_coverage` WHERE `transform_id`={tid}
  ORDER BY `article_id`,`source_ordinal`;
  SET inserted_coverage=ROW_COUNT();

  INSERT INTO `UAT_article_tags` (`article_id`,`tag`)
  SELECT `article_id`,`tag` FROM `UAT_p4_article_tags`
  WHERE `transform_id`={tid} ORDER BY `article_id`,`source_ordinal`;
  SET inserted_tags=ROW_COUNT();

  IF inserted_articles<>expected_articles
     OR inserted_coverage<>expected_coverage
     OR inserted_tags<>expected_tags
     OR (SELECT COUNT(*) FROM `UAT_articles`)<>before_articles+expected_articles
     OR (SELECT COUNT(*) FROM `UAT_article_coverage`)<>before_coverage+expected_coverage
     OR (SELECT COUNT(*) FROM `UAT_article_tags`)<>before_tags+expected_tags
     OR EXISTS (
       SELECT 1 FROM `UAT_article_tags` t
       LEFT JOIN `UAT_articles` a ON a.`article_id`=t.`article_id`
       WHERE a.`article_id` IS NULL
     )
     OR EXISTS (
       SELECT 1 FROM `UAT_article_coverage` c
       LEFT JOIN `UAT_articles` a ON a.`article_id`=c.`article_id`
       WHERE a.`article_id` IS NULL
     )
  THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='Canonical UAT validation failed; load rolled back';
  END IF;

  UPDATE `UAT_p5_load_batches`
  SET `status`='validated',`loaded_article_count`=inserted_articles,
      `loaded_coverage_count`=inserted_coverage,`loaded_media_count`=0,
      `loaded_tag_count`=inserted_tags,`loaded_user_group_count`=0,
      `after_article_count`=before_articles+inserted_articles,
      `after_coverage_count`=before_coverage+inserted_coverage,
      `after_media_count`=`before_media_count`,
      `after_tag_count`=before_tags+inserted_tags,
      `after_user_group_count`=`before_user_group_count`,
      `loaded_at`=UTC_TIMESTAMP(6),`validated_at`=UTC_TIMESTAMP(6)
  WHERE `load_id`={lid};
  COMMIT;
END//
DELIMITER ;
CALL `{procedure}`();
DROP PROCEDURE `{procedure}`;
SELECT * FROM `UAT_p5_load_batches` WHERE `load_id`={lid};
"""


def prepare(args: argparse.Namespace) -> int:
    input_dir = args.input_dir.resolve()
    candidates = list(input_dir.glob("*.md"))
    if not args.loose_only:
        candidates.extend(input_dir.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]/*.md"))
    files = sorted(
        path for path in candidates
        if path.is_file() and path.name != ".DS_Store"
    )
    if not files:
        raise ValueError(f"no Markdown articles found under {input_dir}")
    assessments = load_assessments([path.resolve() for path in args.assessment])
    approvals = load_approvals(args.approvals.resolve() if args.approvals else None)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise ValueError(f"output directory must be empty: {output_dir}")

    source_material = "\n".join(f"{relative_path(path)}\0{sha256_file(path)}" for path in files)
    assessment_material = "\n".join(
        f"{path.resolve()}\0{sha256_file(path.resolve())}" for path in args.assessment
    )
    approval_material = sha256_file(args.approvals.resolve()) if args.approvals else ""
    source_set_hash = sha256_bytes(
        (source_material + "\n" + assessment_material + "\n" + approval_material).encode("utf-8")
    )
    batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"enriched-radar:{source_set_hash}"))
    transform_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{batch_id}:transform"))
    load_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{transform_id}:uat-load"))
    prepared = datetime.now(timezone.utc)
    mapping_hash = sha256_bytes(MAPPING_CONTRACT.encode("utf-8"))

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
        name: (output_dir / filename).open("w", encoding="utf-8", newline="\n")
        for name, filename in names.items()
    }
    counts = Counter()
    ready_counts = Counter()
    review_rows = []
    approval_template: dict[str, Any] = {"approvals": {}}

    try:
        for staging_id, path in enumerate(files, start=1):
            lines, body = split_note(path.read_text(encoding="utf-8"))
            metadata = parse_frontmatter(lines)
            external_id = str(metadata.get("articleId") or "").strip()
            if not external_id:
                external_id = f"missing:{relative_path(path)}"
            synthetic_id = -staging_id
            published = mysql_datetime(metadata.get("publishedDate"))
            assessment = assessments.get(external_id)
            approval = approvals.get(external_id)
            values, problems = assessment_state(
                external_id, metadata, assessment, approval,
            )
            title = str(metadata.get("articleTitle") or "").strip()
            if not title:
                problems.append({
                    "rule": "MISSING_ARTICLE_TITLE", "severity": "error",
                    "field": "articleTitle", "details": "Article title is required.",
                })
            if len(title) > 500:
                problems.append({
                    "rule": "OVERLENGTH_FIELD", "severity": "error",
                    "field": "articleTitle", "details": "Article title exceeds 500 characters.",
                })
            if len(external_id) > 400:
                problems.append({
                    "rule": "OVERLENGTH_FIELD", "severity": "error",
                    "field": "articleId", "details": "External article ID exceeds 400 characters.",
                })
            for tag in values["tags"]:
                if not isinstance(tag, str) or not tag.strip() or len(tag) > 200:
                    problems.append({
                        "rule": "INVALID_TAG", "severity": "error",
                        "field": "tags", "details": f"Invalid tag: {tag!r}",
                    })
                    break

            is_ready = not problems
            status = "ready" if is_ready else (
                "quarantined" if any(row["severity"] == "error" for row in problems) else "review"
            )
            file_hash = sha256_file(path)
            write_tsv_row(
                handles["source_files"],
                [
                    batch_id, staging_id, relative_path(path), path.name, file_hash,
                    path.stat().st_size, 1, published, published,
                ],
            )
            counts["files"] += 1

            raw_record = {
                "externalArticleId": external_id,
                "sourcePath": relative_path(path),
                "inputFrontmatter": metadata,
                "sourceBody": body,
                "approvedValues": values,
                "approval": approval,
                "assessmentArtifactArticleId": external_id if assessment else None,
            }
            article_record_hash = record_hash(raw_record)
            write_tsv_row(
                handles["articles"],
                [
                    batch_id, staging_id, staging_id, 1, synthetic_id, synthetic_id,
                    external_id, title, title, body.strip(), metadata.get("topic"),
                    values["category"], values["tone"], metadata.get("toneSentiment") or "Neutral",
                    values["eventType"], 2, "Article", "NEWS", "A", None,
                    metadata.get("sourceType") or "news", published, published, published,
                    published, "radar-enrichment", "radar-enrichment", "{}", "[]",
                    canonical_json(raw_record), article_record_hash, status,
                ],
            )
            counts["articles"] += 1

            outlet_name = str(values["outletName"] or "").strip()
            outlet_country = str(values["outletCountry"] or "").strip()
            if outlet_name and outlet_country:
                coverage = {
                    "externalArticleId": external_id,
                    "coverageType": "online",
                    "displayName": outlet_name,
                    "country": outlet_country,
                    "url": metadata.get("url"),
                }
                write_tsv_row(
                    handles["coverage"],
                    [
                        batch_id, staging_id, synthetic_id, 1, "online", None,
                        outlet_name, outlet_country, "Online News", metadata.get("url"),
                        record_hash(coverage),
                    ],
                )
                counts["coverage"] += 1
                if is_ready:
                    ready_counts["coverage"] += 1

            for ordinal, tag in enumerate(values["tags"], start=1):
                tag_record = {
                    "externalArticleId": external_id,
                    "sourceOrdinal": ordinal,
                    "tag": tag,
                }
                write_tsv_row(
                    handles["tags"],
                    [batch_id, staging_id, synthetic_id, ordinal, tag, record_hash(tag_record)],
                )
                counts["tags"] += 1
                if is_ready:
                    ready_counts["tags"] += 1

            for problem in problems:
                write_tsv_row(
                    handles["issues"],
                    [
                        batch_id, staging_id, synthetic_id, problem["rule"],
                        problem["severity"], problem["field"],
                        canonical_json(values.get(problem["field"])),
                        problem["details"],
                    ],
                )
                counts["precomputedIssues"] += 1
            if is_ready:
                ready_counts["articles"] += 1
            else:
                counts["quarantinedArticles" if status == "quarantined" else "reviewArticles"] += 1
                reasons = sorted({f"{row['field']}: {row['details']}" for row in problems})
                review_rows.append({
                    "externalArticleId": external_id,
                    "path": relative_path(path),
                    "status": status,
                    "reasons": " | ".join(reasons),
                    "proposedTone": values["tone"],
                    "proposedEventType": values["eventType"],
                    "proposedTags": " | ".join(values["tags"]),
                    "proposedOutlet": outlet_name,
                    "proposedCountry": outlet_country,
                    "proposedCategory": values["category"],
                })
                approval_template["approvals"][external_id] = {
                    "approved": False,
                    "approvedBy": "",
                    "approvedAt": "",
                    "fields": values,
                }
    finally:
        for handle in handles.values():
            handle.close()

    counts["media"] = 0
    counts["userGroups"] = 0
    counts["readyArticles"] = ready_counts["articles"]
    with (output_dir / "review_queue.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "externalArticleId", "path", "status", "reasons", "proposedTone",
            "proposedEventType", "proposedTags", "proposedOutlet", "proposedCountry",
            "proposedCategory",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(review_rows)
    (output_dir / "review_approval_template.json").write_text(
        json.dumps(approval_template, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    batch = {
        "bundleVersion": "enriched-radar-uat-bundle.v1",
        "loaderVersion": LOADER_VERSION,
        "targetDatabase": TARGET_DATABASE,
        "sourceSetName": args.source_set_name,
        "sourceSetHash": source_set_hash,
        "batchId": batch_id,
        "transformId": transform_id,
        "loadId": load_id,
        "mappingContractHash": mapping_hash,
        "preparedAt": prepared.isoformat(timespec="seconds"),
        "preparedAtMysql": prepared.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "counts": dict(counts),
        "readyCounts": dict(ready_counts),
        "assessmentFiles": [relative_path(path.resolve()) for path in args.assessment],
        "approvalFile": relative_path(args.approvals.resolve()) if args.approvals else None,
    }
    phase3_counts = {
        "files": counts["files"],
        "articles": counts["articles"],
        "coverage": counts["coverage"],
        "media": 0,
        "tags": counts["tags"],
        "userGroups": 0,
        "precomputedIssues": counts["precomputedIssues"],
    }
    load_batch = {
        **batch,
        "counts": phase3_counts,
    }
    (output_dir / "phase3_load.sql").write_text(
        render_load_sql(output_dir, load_batch, names), encoding="utf-8",
    )
    (output_dir / "phase3_validate.sql").write_text(
        phase3_validation_sql(batch_id, phase3_counts), encoding="utf-8",
    )
    (output_dir / "phase4_transform.sql").write_text(
        phase4_transform_sql(batch_id, transform_id), encoding="utf-8",
    )
    expected = {
        "readyArticles": ready_counts["articles"],
        "reviewArticles": counts["reviewArticles"],
        "quarantinedArticles": counts["quarantinedArticles"],
        "readyCoverage": ready_counts["coverage"],
        "readyTags": ready_counts["tags"],
    }
    (output_dir / "phase4_validate.sql").write_text(
        phase4_validation_sql(transform_id, expected), encoding="utf-8",
    )
    (output_dir / "phase5_load_uat.sql").write_text(
        phase5_transaction_sql(transform_id, load_id), encoding="utf-8",
    )
    (output_dir / "RUNBOOK.md").write_text(
        f"""# Enriched radar input UAT bundle

This bundle is read-only until its SQL files are explicitly run.

1. Install the shared schemas once, if absent:
   `scripts/sql/phase3_staging_schema.sql`,
   `scripts/sql/phase4_candidate_schema.sql`, and
   `scripts/sql/phase5_load_registry.sql`.
2. Run `phase3_load.sql`.
3. Run `phase3_validate.sql` and require status `validated`.
4. Run `phase4_transform.sql`.
5. Run `phase4_validate.sql` and require status `validated`.
6. Inspect `review_queue.csv`. Reviewed approvals belong in a copy of
   `review_approval_template.json`; re-run the stager with `--approvals`.
7. Run `phase5_load_uat.sql`. It loads and validates in one transaction and
   rolls back automatically if any gate fails.
8. Run `python3 scripts/issue_radar.py --source uat ...`.

The stager includes loose files and month subfolders by default. Use
`--loose-only` only when the approved batch is explicitly limited to loose
files at the Inputs root.

Batch: `{batch_id}`
Transform: `{transform_id}`
Load: `{load_id}`
Automatically ready: {ready_counts['articles']}
Review: {counts['reviewArticles']}
Quarantined: {counts['quarantinedArticles']}
""",
        encoding="utf-8",
    )

    bundle_files = {}
    for path in sorted(output_dir.iterdir()):
        if path.name == "bundle_manifest.json":
            continue
        rows = None
        if path.suffix in {".tsv", ".csv"}:
            with path.open(encoding="utf-8") as handle:
                rows = sum(1 for _ in handle)
        bundle_files[path.name] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            "rows": rows,
        }
    batch["bundleFiles"] = bundle_files
    (output_dir / "bundle_manifest.json").write_text(
        json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({
        "status": "prepared",
        "outputDir": str(output_dir),
        "articles": counts["articles"],
        "ready": ready_counts["articles"],
        "review": counts["reviewArticles"],
        "quarantined": counts["quarantinedArticles"],
        "tags": counts["tags"],
        "coverage": counts["coverage"],
    }, indent=2))
    return 0


def verify(args: argparse.Namespace) -> int:
    bundle_dir = args.bundle_dir.resolve()
    manifest = json.loads((bundle_dir / "bundle_manifest.json").read_text(encoding="utf-8"))
    errors = []
    for filename, expected in manifest["bundleFiles"].items():
        path = bundle_dir / filename
        if not path.is_file():
            errors.append(f"missing file: {filename}")
            continue
        if sha256_file(path) != expected["sha256"]:
            errors.append(f"hash mismatch: {filename}")
        if expected["rows"] is not None:
            with path.open(encoding="utf-8") as handle:
                actual_rows = sum(1 for _ in handle)
            if actual_rows != expected["rows"]:
                errors.append(f"row mismatch: {filename}")
    result = {
        "status": "passed" if not errors else "failed",
        "batchId": manifest["batchId"],
        "errors": errors,
    }
    print(json.dumps(result, indent=2))
    return 0 if not errors else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--input-dir", type=Path, default=ROOT / "Inputs" / "articles")
    prepare_parser.add_argument(
        "--loose-only", action="store_true",
        help="include only files directly under input-dir; default includes month subfolders",
    )
    prepare_parser.add_argument("--assessment", type=Path, action="append", required=True)
    prepare_parser.add_argument("--approvals", type=Path)
    prepare_parser.add_argument("--output-dir", type=Path, required=True)
    prepare_parser.add_argument(
        "--source-set-name", default="Enriched radar inputs from Markdown",
    )
    prepare_parser.set_defaults(func=prepare)
    verify_parser = subparsers.add_parser("verify-bundle")
    verify_parser.add_argument("--bundle-dir", type=Path, required=True)
    verify_parser.set_defaults(func=verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
