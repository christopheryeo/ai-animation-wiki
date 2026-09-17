#!/usr/bin/env python3
"""Reconcile two independent local Codex enrichment reviews without data egress."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from enrich_radar_inputs import (
    EVENT_VALUES,
    INSTITUTIONAL,
    SENTIMENT_VALUES,
    TONE_VALUES,
    consensus,
    load_tag_inventory,
    parse_frontmatter,
    split_note,
)


ROOT = Path(__file__).resolve().parents[1]
INPUT_ROOT = ROOT / "Inputs" / "articles"


class ReviewError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ReviewError(f"JSON root must be an object: {path}")
    return value


def index_review(path: Path) -> dict[str, dict[str, Any]]:
    payload = load_json(path)
    rows = payload.get("assessments")
    if not isinstance(rows, list):
        raise ReviewError(f"assessments must be a list: {path}")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewError(f"assessment must be an object: {path}")
        article_id = str(row.get("articleId") or "")
        if not article_id or article_id in indexed:
            raise ReviewError(f"missing or duplicate articleId {article_id!r}: {path}")
        indexed[article_id] = row
    return indexed


def index_sentiment_adjudication(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payload = load_json(path)
    policy_version = str(payload.get("policyVersion") or "").strip()
    if not policy_version:
        raise ReviewError(f"sentiment adjudication policyVersion is missing: {path}")
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ReviewError(f"sentiment decisions must be a list: {path}")
    if payload.get("inputCount") != len(rows) or payload.get("adjudicatedCount") != len(rows):
        raise ReviewError(f"sentiment adjudication count is invalid: {path}")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewError(f"sentiment decision must be an object: {path}")
        article_id = str(row.get("articleId") or "")
        if not article_id or article_id in indexed:
            raise ReviewError(f"missing or duplicate sentiment articleId {article_id!r}: {path}")
        if row.get("toneSentiment") not in SENTIMENT_VALUES:
            raise ReviewError(
                f"{article_id}: invalid adjudicated sentiment={row.get('toneSentiment')!r}"
            )
        confidence = row.get("confidence")
        if (
            not isinstance(confidence, (int, float))
            or isinstance(confidence, bool)
            or not 0 <= confidence <= 1
        ):
            raise ReviewError(f"{article_id}: invalid adjudicated confidence={confidence!r}")
        if not str(row.get("evidence") or "").strip():
            raise ReviewError(f"{article_id}: adjudicated sentiment evidence is empty")
        if not str(row.get("rationale") or "").strip():
            raise ReviewError(f"{article_id}: adjudicated sentiment rationale is empty")
        if not str(row.get("path") or "").strip():
            raise ReviewError(f"{article_id}: adjudicated sentiment path is empty")
        indexed[article_id] = row
    return indexed, policy_version


def apply_sentiment_adjudication(
    article_id: str,
    merged: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    reason = "tone-sentiment disagreement or low confidence"
    if reason not in merged["reviewReasons"]:
        raise ReviewError(f"{article_id}: sentiment adjudication supplied without a sentiment hold")
    merged["toneSentiment"] = decision["toneSentiment"]
    merged["sentimentConfidence"] = decision["confidence"]
    merged["sentimentEvidence"] = [decision["evidence"]]
    merged["autoApplicable"]["toneSentiment"] = True
    merged["reviewReasons"] = [item for item in merged["reviewReasons"] if item != reason]
    merged["sentimentAdjudication"] = {
        "toneSentiment": decision["toneSentiment"],
        "confidence": decision["confidence"],
        "evidence": decision["evidence"],
        "rationale": decision["rationale"],
    }


def index_tone_adjudication(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payload = load_json(path)
    policy_version = str(payload.get("policyVersion") or "").strip()
    if not policy_version:
        raise ReviewError(f"tone adjudication policyVersion is missing: {path}")
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ReviewError(f"tone decisions must be a list: {path}")
    if payload.get("inputCount") != len(rows) or payload.get("adjudicatedCount") != len(rows):
        raise ReviewError(f"tone adjudication count is invalid: {path}")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewError(f"tone decision must be an object: {path}")
        article_id = str(row.get("articleId") or "")
        if not article_id or article_id in indexed:
            raise ReviewError(f"missing or duplicate tone articleId {article_id!r}: {path}")
        _validate_decision_common(article_id, row, "tone")
        if row.get("tone") not in TONE_VALUES:
            raise ReviewError(f"{article_id}: invalid adjudicated tone={row.get('tone')!r}")
        indexed[article_id] = row
    return indexed, policy_version


def apply_tone_adjudication(
    article_id: str,
    merged: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    reason = "tone disagreement or low confidence"
    if reason not in merged["reviewReasons"]:
        raise ReviewError(f"{article_id}: tone adjudication supplied without a tone hold")
    merged["tone"] = decision["tone"]
    merged["toneConfidence"] = decision["confidence"]
    merged["toneEvidence"] = [decision["evidence"]]
    merged["autoApplicable"]["tone"] = True
    merged["reviewReasons"] = [item for item in merged["reviewReasons"] if item != reason]
    merged["toneAdjudication"] = {
        "tone": decision["tone"],
        "confidence": decision["confidence"],
        "evidence": decision["evidence"],
        "rationale": decision["rationale"],
    }


def _validate_decision_common(
    article_id: str,
    row: dict[str, Any],
    kind: str,
) -> None:
    confidence = row.get("confidence")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not 0 <= confidence <= 1
    ):
        raise ReviewError(f"{article_id}: invalid {kind} confidence={confidence!r}")
    for field in ["path", "evidence", "rationale"]:
        if not str(row.get(field) or "").strip():
            raise ReviewError(f"{article_id}: {kind} {field} is empty")


def index_metadata_adjudication(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payload = load_json(path)
    policy_version = str(payload.get("policyVersion") or "").strip()
    if not policy_version:
        raise ReviewError(f"metadata adjudication policyVersion is missing: {path}")
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ReviewError(f"metadata decisions must be a list: {path}")
    if payload.get("inputCount") != len(rows) or payload.get("adjudicatedCount") != len(rows):
        raise ReviewError(f"metadata adjudication count is invalid: {path}")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewError(f"metadata decision must be an object: {path}")
        article_id = str(row.get("articleId") or "")
        if not article_id or article_id in indexed:
            raise ReviewError(f"missing or duplicate metadata articleId {article_id!r}: {path}")
        _validate_decision_common(article_id, row, "metadata")
        if not str(row.get("outletName") or "").strip():
            raise ReviewError(f"{article_id}: adjudicated outletName is empty")
        if not str(row.get("outletId") or "").strip():
            raise ReviewError(f"{article_id}: adjudicated outletId is empty")
        if not isinstance(row.get("outletCountry"), str):
            raise ReviewError(f"{article_id}: adjudicated outletCountry must be a string")
        if row.get("institutionalCategory") not in INSTITUTIONAL:
            raise ReviewError(
                f"{article_id}: invalid institutionalCategory={row.get('institutionalCategory')!r}"
            )
        if not str(row.get("sourceBasis") or "").strip():
            raise ReviewError(f"{article_id}: metadata sourceBasis is empty")
        if not isinstance(row.get("originalAgency", ""), str):
            raise ReviewError(f"{article_id}: originalAgency must be a string")
        indexed[article_id] = row
    return indexed, policy_version


def apply_metadata_adjudication(
    article_id: str,
    merged: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    reason = "metadata disagreement or low confidence"
    if reason not in merged["reviewReasons"]:
        raise ReviewError(f"{article_id}: metadata adjudication supplied without a metadata hold")
    merged["outletName"] = decision["outletName"]
    merged["outletId"] = decision["outletId"]
    merged["outletCountry"] = decision["outletCountry"]
    merged["institutionalCategory"] = decision["institutionalCategory"]
    merged["metadataConfidence"] = decision["confidence"]
    merged["autoApplicable"]["metadata"] = True
    merged["reviewReasons"] = [item for item in merged["reviewReasons"] if item != reason]
    merged["metadataAdjudication"] = {
        "outletName": decision["outletName"],
        "outletId": decision["outletId"],
        "outletCountry": decision["outletCountry"],
        "institutionalCategory": decision["institutionalCategory"],
        "confidence": decision["confidence"],
        "evidence": decision["evidence"],
        "sourceBasis": decision["sourceBasis"],
        "originalAgency": decision.get("originalAgency", ""),
        "rationale": decision["rationale"],
    }


def index_event_adjudication(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    payload = load_json(path)
    policy_version = str(payload.get("policyVersion") or "").strip()
    if not policy_version:
        raise ReviewError(f"event adjudication policyVersion is missing: {path}")
    rows = payload.get("decisions")
    if not isinstance(rows, list):
        raise ReviewError(f"event decisions must be a list: {path}")
    if payload.get("inputCount") != len(rows) or payload.get("adjudicatedCount") != len(rows):
        raise ReviewError(f"event adjudication count is invalid: {path}")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewError(f"event decision must be an object: {path}")
        article_id = str(row.get("articleId") or "")
        if not article_id or article_id in indexed:
            raise ReviewError(f"missing or duplicate event articleId {article_id!r}: {path}")
        _validate_decision_common(article_id, row, "event")
        if row.get("eventType") not in EVENT_VALUES:
            raise ReviewError(f"{article_id}: invalid eventType={row.get('eventType')!r}")
        if not str(row.get("eventTrigger") or "").strip():
            raise ReviewError(f"{article_id}: eventTrigger is empty")
        indexed[article_id] = row
    return indexed, policy_version


def apply_event_adjudication(
    article_id: str,
    merged: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    reason = "event-type disagreement or low confidence"
    if reason not in merged["reviewReasons"]:
        raise ReviewError(f"{article_id}: event adjudication supplied without an event hold")
    merged["eventType"] = decision["eventType"]
    merged["eventConfidence"] = decision["confidence"]
    merged["eventTrigger"] = decision["eventTrigger"]
    merged["eventEvidence"] = [decision["evidence"]]
    merged["autoApplicable"]["eventType"] = True
    merged["reviewReasons"] = [item for item in merged["reviewReasons"] if item != reason]
    merged["eventAdjudication"] = {
        "eventType": decision["eventType"],
        "confidence": decision["confidence"],
        "eventTrigger": decision["eventTrigger"],
        "evidence": decision["evidence"],
        "rationale": decision["rationale"],
    }


def reviewer_request_resolved(
    classification: dict[str, Any],
    has_sentiment: bool,
    has_metadata: bool,
    has_event: bool,
    has_tone: bool = False,
) -> bool:
    if not classification.get("review_required"):
        return True
    reason = str(classification.get("review_reason") or "").casefold()
    keyword_groups = []
    if has_sentiment:
        keyword_groups.append(("sentiment", "positive", "negative", "neutral"))
    if has_metadata:
        keyword_groups.append(("metadata", "outlet", "publisher", "country", "institutional"))
    if has_event:
        keyword_groups.append(("event", "trigger", "facilitated", "unfacilitated", "news peg"))
    if has_tone:
        keyword_groups.append(("tone", "factual", "opinionated", "framing", "judgement", "judgment"))
    return bool(reason) and any(keyword in reason for group in keyword_groups for keyword in group)


def validate_classification(
    article_id: str,
    value: dict[str, Any],
    allowed_tags: set[str],
) -> None:
    enum_fields = {
        "tone": set(TONE_VALUES),
        "tone_sentiment": set(SENTIMENT_VALUES),
        "event_type": set(EVENT_VALUES),
        "institutional_category": set(INSTITUTIONAL),
    }
    for field, allowed in enum_fields.items():
        if value.get(field) not in allowed:
            raise ReviewError(f"{article_id}: invalid {field}={value.get(field)!r}")
    for field in ["tone_confidence", "sentiment_confidence", "event_confidence", "metadata_confidence"]:
        score = value.get(field)
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1:
            raise ReviewError(f"{article_id}: invalid {field}={score!r}")
    for field in ["tone_evidence", "sentiment_evidence", "event_evidence", "issue_tags"]:
        if not isinstance(value.get(field), list):
            raise ReviewError(f"{article_id}: {field} must be a list")
    unknown_tags = sorted(set(value["issue_tags"]) - allowed_tags)
    if unknown_tags:
        raise ReviewError(f"{article_id}: tags outside inventory: {unknown_tags}")
    if not str(value.get("outlet_name") or "").strip():
        raise ReviewError(f"{article_id}: outlet_name is empty")
    if not isinstance(value.get("review_required"), bool):
        raise ReviewError(f"{article_id}: review_required must be boolean")


def locate_input(filename: str) -> Path:
    matches = sorted(INPUT_ROOT.glob(f"????-??/{filename}"))
    if len(matches) != 1:
        raise ReviewError(f"expected one routed input for {filename}, found {len(matches)}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest-evidence", type=Path, required=True)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--accepted-manifest", type=Path, required=True)
    parser.add_argument("--held-manifest", type=Path, required=True)
    parser.add_argument("--hold-output", type=Path, required=True)
    parser.add_argument(
        "--restrict-manifest",
        type=Path,
        help="resume only the filenames listed in this frozen manifest while retaining original evidence hashes",
    )
    parser.add_argument(
        "--sentiment-adjudication",
        type=Path,
        help=(
            "complete third-AI decisions for every two-review sentiment hold; "
            "uses the accepted Christopher Yeo sentiment policy"
        ),
    )
    parser.add_argument(
        "--tone-adjudication",
        type=Path,
        help=(
            "complete third-AI decisions for every two-review Factual/Opinionated hold; "
            "uses the accepted Christopher Yeo tone policy"
        ),
    )
    parser.add_argument(
        "--metadata-adjudication",
        type=Path,
        help=(
            "complete third-AI decisions for every two-review metadata hold; "
            "uses the accepted Christopher Yeo outlet and institutional-category policy"
        ),
    )
    parser.add_argument(
        "--event-adjudication",
        type=Path,
        help=(
            "complete third-AI decisions for every two-review event-type hold; "
            "uses the accepted Christopher Yeo publication-trigger policy"
        ),
    )
    parser.add_argument("--confidence", type=float, default=0.82)
    args = parser.parse_args()

    manifest = load_json(args.manifest_evidence.resolve())
    articles = manifest.get("articles")
    if not isinstance(articles, list) or manifest.get("articleCount") != len(articles):
        raise ReviewError("manifest article count is invalid")
    if args.restrict_manifest:
        selected = {
            Path(line.strip()).name
            for line in args.restrict_manifest.resolve().read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        if not selected:
            raise ReviewError("restrict manifest is empty")
        available = {str(item.get("filename") or "") for item in articles}
        missing = sorted(selected - available)
        if missing:
            raise ReviewError(f"restrict manifest filenames are absent from evidence: {missing[:5]}")
        articles = [item for item in articles if str(item.get("filename") or "") in selected]
    manifest_by_id = {str(item.get("articleId") or ""): item for item in articles}
    if len(manifest_by_id) != len(articles) or "" in manifest_by_id:
        raise ReviewError("manifest article IDs are missing or duplicated")

    primary = index_review(args.primary.resolve())
    review = index_review(args.review.resolve())
    sentiment_decisions: dict[str, dict[str, Any]] = {}
    sentiment_policy_version = ""
    if args.sentiment_adjudication:
        sentiment_decisions, sentiment_policy_version = index_sentiment_adjudication(
            args.sentiment_adjudication.resolve()
        )
    tone_decisions: dict[str, dict[str, Any]] = {}
    tone_policy_version = ""
    if args.tone_adjudication:
        tone_decisions, tone_policy_version = index_tone_adjudication(
            args.tone_adjudication.resolve()
        )
    metadata_decisions: dict[str, dict[str, Any]] = {}
    metadata_policy_version = ""
    if args.metadata_adjudication:
        metadata_decisions, metadata_policy_version = index_metadata_adjudication(
            args.metadata_adjudication.resolve()
        )
    event_decisions: dict[str, dict[str, Any]] = {}
    event_policy_version = ""
    if args.event_adjudication:
        event_decisions, event_policy_version = index_event_adjudication(
            args.event_adjudication.resolve()
        )
    expected = set(manifest_by_id)
    primary = {article_id: primary[article_id] for article_id in expected if article_id in primary}
    review = {article_id: review[article_id] for article_id in expected if article_id in review}
    if set(primary) != expected or set(review) != expected:
        raise ReviewError(
            f"review coverage mismatch: manifest={len(expected)} primary={len(primary)} review={len(review)}"
        )

    allowed_tags = {row[0] for row in load_tag_inventory()}
    assessments = []
    accepted_paths = []
    held_paths = []
    holds = []
    sentiment_holds: set[str] = set()
    tone_holds: set[str] = set()
    metadata_holds: set[str] = set()
    event_holds: set[str] = set()
    for article_id in sorted(expected):
        manifest_row = manifest_by_id[article_id]
        path = locate_input(str(manifest_row["filename"]))
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != manifest_row["sha256"]:
            raise ReviewError(f"manifest hash drift: {path}")
        primary_value = primary[article_id].get("classification")
        review_value = review[article_id].get("classification")
        if not isinstance(primary_value, dict) or not isinstance(review_value, dict):
            raise ReviewError(f"{article_id}: classification must be an object")
        validate_classification(article_id, primary_value, allowed_tags)
        validate_classification(article_id, review_value, allowed_tags)
        merged = consensus(primary_value, review_value, args.confidence, allowed_tags)
        if "tone-sentiment disagreement or low confidence" in merged["reviewReasons"]:
            sentiment_holds.add(article_id)
        if "tone disagreement or low confidence" in merged["reviewReasons"]:
            tone_holds.add(article_id)
        if "metadata disagreement or low confidence" in merged["reviewReasons"]:
            metadata_holds.add(article_id)
        if "event-type disagreement or low confidence" in merged["reviewReasons"]:
            event_holds.add(article_id)
        note_text = path.read_text(encoding="utf-8")
        if article_id in sentiment_decisions:
            decision = sentiment_decisions[article_id]
            relative_path = str(path.relative_to(ROOT))
            if decision["path"] != relative_path:
                raise ReviewError(
                    f"{article_id}: adjudication path mismatch "
                    f"expected={relative_path!r} supplied={decision['path']!r}"
                )
            if decision["evidence"] not in note_text:
                raise ReviewError(f"{article_id}: adjudicated evidence is not in the saved input")
            apply_sentiment_adjudication(article_id, merged, decision)
        if article_id in tone_decisions:
            decision = tone_decisions[article_id]
            relative_path = str(path.relative_to(ROOT))
            if decision["path"] != relative_path:
                raise ReviewError(
                    f"{article_id}: tone adjudication path mismatch "
                    f"expected={relative_path!r} supplied={decision['path']!r}"
                )
            if decision["evidence"] not in note_text:
                raise ReviewError(f"{article_id}: adjudicated tone evidence is not in the saved input")
            apply_tone_adjudication(article_id, merged, decision)
        if article_id in metadata_decisions:
            decision = metadata_decisions[article_id]
            relative_path = str(path.relative_to(ROOT))
            if decision["path"] != relative_path:
                raise ReviewError(
                    f"{article_id}: metadata adjudication path mismatch "
                    f"expected={relative_path!r} supplied={decision['path']!r}"
                )
            if decision["evidence"] not in note_text:
                raise ReviewError(f"{article_id}: adjudicated metadata evidence is not in the saved input")
            apply_metadata_adjudication(article_id, merged, decision)
        if article_id in event_decisions:
            decision = event_decisions[article_id]
            relative_path = str(path.relative_to(ROOT))
            if decision["path"] != relative_path:
                raise ReviewError(
                    f"{article_id}: event adjudication path mismatch "
                    f"expected={relative_path!r} supplied={decision['path']!r}"
                )
            if decision["evidence"] not in note_text:
                raise ReviewError(f"{article_id}: adjudicated event evidence is not in the saved input")
            apply_event_adjudication(article_id, merged, decision)
        reviewer_hold = any(
            not reviewer_request_resolved(
                value,
                article_id in sentiment_decisions,
                article_id in metadata_decisions,
                article_id in event_decisions,
                article_id in tone_decisions,
            )
            for value in [primary_value, review_value]
        )
        if not reviewer_hold:
            merged["reviewReasons"] = [
                reason for reason in merged["reviewReasons"]
                if reason not in {"model requested review", "local reviewer requested review"}
            ]
        if reviewer_hold:
            merged["readyForCascade"] = False
            merged["reviewRequired"] = True
            merged["reviewReasons"] = sorted(set(
                merged["reviewReasons"] + ["local reviewer requested review"]
            ))
        merged["reviewRequired"] = bool(merged["reviewReasons"])
        merged["readyForCascade"] = (
            not merged["reviewRequired"]
            and all(
                merged["autoApplicable"].get(field, False)
                for field in ["tone", "toneSentiment", "eventType", "metadata"]
            )
        )

        lines, _ = split_note(note_text)
        metadata = parse_frontmatter(lines)
        assessment = {
            "path": str(path.relative_to(ROOT)),
            "articleId": article_id,
            "inputSha256": actual_hash,
            "url": str(metadata.get("url") or ""),
            "inputSignals": {
                "relevant": metadata.get("relevant"),
                "relevanceConfidence": metadata.get("relevance_confidence"),
                "relevanceReason": metadata.get("relevance_reason"),
                "duplicateFlag": metadata.get("duplicateFlag"),
                "duplicateList": metadata.get("duplicateList"),
            },
            "sourceTextStatus": "saved-input-only",
            "primary": primary_value,
            "review": review_value,
            "consensus": merged,
            "appliedFields": [],
        }
        assessments.append(assessment)
        relative = str(path.relative_to(ROOT))
        if merged["readyForCascade"]:
            accepted_paths.append(relative)
        else:
            held_paths.append(relative)
            holds.append({
                "articleId": article_id,
                "path": relative,
                "reasons": merged["reviewReasons"],
                "inputSignals": assessment["inputSignals"],
            })

    if sentiment_decisions and set(sentiment_decisions) != sentiment_holds:
        missing = sorted(sentiment_holds - set(sentiment_decisions))
        extra = sorted(set(sentiment_decisions) - sentiment_holds)
        raise ReviewError(
            "sentiment adjudication coverage mismatch: "
            f"needed={len(sentiment_holds)} supplied={len(sentiment_decisions)} "
            f"missing={missing[:5]} extra={extra[:5]}"
        )
    if tone_decisions and set(tone_decisions) != tone_holds:
        missing = sorted(tone_holds - set(tone_decisions))
        extra = sorted(set(tone_decisions) - tone_holds)
        raise ReviewError(
            "tone adjudication coverage mismatch: "
            f"needed={len(tone_holds)} supplied={len(tone_decisions)} "
            f"missing={missing[:5]} extra={extra[:5]}"
        )
    if metadata_decisions and set(metadata_decisions) != metadata_holds:
        missing = sorted(metadata_holds - set(metadata_decisions))
        extra = sorted(set(metadata_decisions) - metadata_holds)
        raise ReviewError(
            "metadata adjudication coverage mismatch: "
            f"needed={len(metadata_holds)} supplied={len(metadata_decisions)} "
            f"missing={missing[:5]} extra={extra[:5]}"
        )
    if event_decisions and set(event_decisions) != event_holds:
        missing = sorted(event_holds - set(event_decisions))
        extra = sorted(set(event_decisions) - event_holds)
        raise ReviewError(
            "event adjudication coverage mismatch: "
            f"needed={len(event_holds)} supplied={len(event_decisions)} "
            f"missing={missing[:5]} extra={extra[:5]}"
        )

    output = {
        "schemaVersion": "radar-input-enrichment.v2",
        "promptVersion": "local-codex-two-pass.v4",
        "model": "Codex in-app independent reviewers",
        "confidenceThreshold": args.confidence,
        "inputCount": len(assessments),
        "assessedCount": len(assessments),
        "failedCount": 0,
        "readyCount": len(accepted_paths),
        "reviewRequiredCount": len(held_paths),
        "sentimentAdjudication": {
            "path": str(args.sentiment_adjudication) if args.sentiment_adjudication else None,
            "policyVersion": sentiment_policy_version or None,
            "decisionCount": len(sentiment_decisions),
        },
        "toneAdjudication": {
            "path": str(args.tone_adjudication) if args.tone_adjudication else None,
            "policyVersion": tone_policy_version or None,
            "decisionCount": len(tone_decisions),
        },
        "metadataAdjudication": {
            "path": str(args.metadata_adjudication) if args.metadata_adjudication else None,
            "policyVersion": metadata_policy_version or None,
            "decisionCount": len(metadata_decisions),
        },
        "eventAdjudication": {
            "path": str(args.event_adjudication) if args.event_adjudication else None,
            "policyVersion": event_policy_version or None,
            "decisionCount": len(event_decisions),
        },
        "assessments": assessments,
        "failures": [],
    }
    for path in [args.output, args.accepted_manifest, args.held_manifest, args.hold_output]:
        path.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.output.resolve().write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.accepted_manifest.resolve().write_text("\n".join(accepted_paths) + ("\n" if accepted_paths else ""), encoding="utf-8")
    args.held_manifest.resolve().write_text("\n".join(held_paths) + ("\n" if held_paths else ""), encoding="utf-8")
    args.hold_output.resolve().write_text(json.dumps({
        "schemaVersion": "enrichment-review-holds.v1",
        "heldCount": len(holds),
        "holds": holds,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "assessed": len(assessments),
        "ready": len(accepted_paths),
        "held": len(held_paths),
        "output": str(args.output),
        "holdOutput": str(args.hold_output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
