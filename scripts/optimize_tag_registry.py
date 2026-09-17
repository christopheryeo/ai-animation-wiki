#!/usr/bin/env python3
"""Analyze and approval-gate Tag radar-eligibility transitions."""

from __future__ import annotations

import argparse
from collections import defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

from issue_radar import STOP
from migrate_tag_entities import article_records
from tag_registry import (
    RADAR_ALLOWED_TRANSITIONS,
    ROOT,
    TAG_ROOT,
    load_registry,
    normalize_tag,
    section,
)


BENCHMARKS = ROOT / "tests" / "fixtures" / "issue_radar_benchmarks.json"
ENTITY_DOMAINS = ("people", "organisations", "place", "country", "appointments", "outlet")
RADAR_SECTION = re.compile(
    r"(?P<head>\n## Radar Status History\s*\n(?P<body>.*?))(?=\n## |\Z)", re.DOTALL
)


class OptimizationError(RuntimeError):
    pass


def frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    return yaml.safe_load(text[4:end]) or {}


def entity_names() -> set[str]:
    names: set[str] = set()
    for domain in ENTITY_DOMAINS:
        for path in (ROOT / "entities" / domain).glob("**/*.md"):
            if path.name in {"index.md", "catalog.md", "log.md", "_template.md"}:
                continue
            data = frontmatter(path)
            for value in (data.get("displayName"), *(data.get("aliases") or [])):
                if value:
                    names.add(normalize_tag(value))
    return names


def protected_tags() -> set[str]:
    benchmark = json.loads(BENCHMARKS.read_text(encoding="utf-8"))
    protected_issue_ids = {
        item["issueId"]
        for item in benchmark["benchmarks"]
        if item.get("expectedMinimumRamification") in {"high", "severe"}
    }
    protected: set[str] = set()
    for path in (ROOT / "entities" / "issues").glob("*.md"):
        if path.name in {"index.md", "catalog.md", "log.md", "_template.md"}:
            continue
        data = frontmatter(path)
        if data.get("issueId") in protected_issue_ids or data.get("ramification") in {"high", "severe"}:
            protected.update(normalize_tag(value) for value in (data.get("clusterTags") or []))
    return protected


def flagged_tags(paths: list[Path]) -> set[str]:
    values: set[str] = set()
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        values.update(normalize_tag(item["tag"]) for item in payload.get("flags", []))
    return values


def article_metrics() -> tuple[dict[str, dict[str, Any]], dt.date]:
    metrics: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"articles": set(), "outlets": set(), "countries": set(), "dates": []}
    )
    latest = dt.date.min
    for record in article_records():
        projection = record["projection"]
        published = str((projection.get("article") or {}).get("published_date") or "")[:10]
        try:
            day = dt.date.fromisoformat(published)
        except ValueError:
            day = None
        if day:
            latest = max(latest, day)
        outlets = {
            normalize_tag(row.get("display_name"))
            for row in projection.get("coverage", [])
            if row.get("display_name")
        }
        countries = {
            normalize_tag(row.get("country"))
            for row in projection.get("coverage", [])
            if row.get("country")
        }
        article_id = str((projection.get("article") or {}).get("article_id") or record["path"])
        for display in set(record["tags"]):
            value = metrics[normalize_tag(display)]
            value["articles"].add(article_id)
            value["outlets"].update(outlets)
            value["countries"].update(countries)
            if day:
                value["dates"].append(day)
    return metrics, latest


def priority(article_count: int) -> int:
    if article_count <= 2:
        return 5
    if article_count <= 5:
        return 4
    if article_count <= 10:
        return 3
    if article_count <= 20:
        return 2
    if article_count <= 50:
        return 1
    return 0


def analyze(target_percent: float, radar_artifacts: list[Path]) -> dict[str, Any]:
    records = load_registry()
    metrics, latest = article_metrics()
    entities = entity_names()
    protected = protected_tags()
    historical_flags = flagged_tags(radar_artifacts)
    signatures: dict[tuple[str, ...], list[str]] = defaultdict(list)
    for record in records:
        values = metrics.get(normalize_tag(record.display_name), {})
        signatures[tuple(sorted(values.get("articles", set())))].append(record.tag_id)

    ranked = []
    for record in records:
        name = normalize_tag(record.display_name)
        values = metrics.get(name, {"articles": set(), "outlets": set(), "countries": set(), "dates": []})
        dates = values["dates"]
        last_seen = max(dates) if dates else None
        reasons: list[str] = []
        score = priority(record.article_count)
        if record.article_count <= 10:
            reasons.append("low-usage: ten or fewer articles")
        if name not in historical_flags:
            score += 2
            reasons.append("no flag in supplied baseline artifacts")
        semantic_role = "issue-or-descriptor"
        if name in entities:
            semantic_role = "entity-like"
            score += 2
            reasons.append("matches a governed non-Tag entity")
        if name in STOP:
            semantic_role = "generic"
            score += 4
            reasons.append("matches the radar generic-stop vocabulary")
        outlet_breadth = len(values["outlets"])
        country_breadth = len(values["countries"])
        if outlet_breadth <= 1:
            score += 1
            reasons.append("outlet breadth is one or less")
        if country_breadth <= 1:
            score += 1
            reasons.append("country breadth is one or less")
        stale = bool(last_seen and latest != dt.date.min and (latest - last_seen).days > 180)
        if stale:
            score += 1
            reasons.append("not seen in the latest 180 days")
        signature = tuple(sorted(values["articles"]))
        exact_coverage_peers = sorted(value for value in signatures[signature] if value != record.tag_id)
        if exact_coverage_peers:
            score += 1
            reasons.append("shares its exact article set with another tag")
        ranked.append({
            "tagId": record.tag_id,
            "displayName": record.display_name,
            "fromRadarStatus": record.radar_status,
            "proposedRadarStatus": "shadow",
            "protected": name in protected,
            "priorityScore": score,
            "reasons": reasons,
            "evidence": {
                "articleCount": record.article_count,
                "outletBreadth": outlet_breadth,
                "countryBreadth": country_breadth,
                "lastSeen": last_seen.isoformat() if last_seen else None,
                "historicallyFlagged": name in historical_flags,
                "semanticRole": semantic_role,
                "exactCoveragePeers": exact_coverage_peers[:10],
            },
        })
    eligible = [
        item for item in ranked
        if not item["protected"] and item["fromRadarStatus"] == "enabled"
    ]
    eligible.sort(key=lambda item: (-item["priorityScore"], item["evidence"]["articleCount"], item["displayName"].casefold()))
    target_count = int(len(records) * target_percent / 100)
    proposals = eligible[:target_count]
    canonical = json.dumps(proposals, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "schemaVersion": "tag-radar-optimization-ledger.v1",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "targetPercent": target_percent,
        "tagCount": len(records),
        "targetCount": target_count,
        "proposalCount": len(proposals),
        "protectedCount": sum(item["protected"] for item in ranked),
        "latestArticleDate": latest.isoformat(),
        "radarArtifacts": [str(path) for path in radar_artifacts],
        "proposalManifestSha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "proposals": proposals,
        "databaseWrites": 0,
        "productionWrites": 0,
    }


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Tag Radar Optimization Proposal",
        "",
        f"- Tag entities: {payload['tagCount']}",
        f"- Proposed shadow tags: {payload['proposalCount']} ({payload['targetPercent']:.1f}%)",
        f"- Protected tags: {payload['protectedCount']}",
        f"- Manifest SHA-256: `{payload['proposalManifestSha256']}`",
        "- Status: awaiting exact attributed approval",
        "",
        "| Tag | Articles | Outlets | Countries | Score | Reasons |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for item in payload["proposals"]:
        evidence = item["evidence"]
        reasons = "; ".join(item["reasons"]).replace("|", "\\|")
        lines.append(
            f"| {item['displayName'].replace('|', '\\|')} | {evidence['articleCount']} | "
            f"{evidence['outletBreadth']} | {evidence['countryBreadth']} | "
            f"{item['priorityScore']} | {reasons} |"
        )
    return "\n".join(lines) + "\n"


def prepare_disable(shadow_ledger_path: Path, verification_path: Path) -> dict[str, Any]:
    shadow_ledger = json.loads(shadow_ledger_path.read_text(encoding="utf-8"))
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    if verification.get("result") != "PASS":
        raise OptimizationError("shadow verification did not pass")
    records = {record.tag_id: record for record in load_registry()}
    proposals = []
    for item in shadow_ledger.get("proposals") or []:
        record = records.get(item["tagId"])
        if record is None or record.radar_status != "shadow":
            raise OptimizationError(f"Tag is not in the approved shadow set: {item['tagId']}")
        proposals.append({
            **item,
            "fromRadarStatus": "shadow",
            "proposedRadarStatus": "disabled",
        })
    return {
        "schemaVersion": "tag-radar-disable-ledger.v1",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "proposalCount": len(proposals),
        "shadowVerification": str(verification_path),
        "shadowVerificationSha256": hashlib.sha256(verification_path.read_bytes()).hexdigest(),
        "proposalManifestSha256": proposal_hash(proposals),
        "proposals": proposals,
        "databaseWrites": 0,
        "productionWrites": 0,
    }


def proposal_hash(proposals: list[dict[str, Any]]) -> str:
    canonical = json.dumps(proposals, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def transition_text(
    text: str, from_status: str, target_status: str, effective_at: str,
    actor: str, reason: str,
) -> str:
    current = re.search(r"^radarStatus:\s*(\S+)\s*$", text, re.MULTILINE)
    if not current or current.group(1) != from_status:
        raise OptimizationError(
            f"radarStatus precondition differs: expected {from_status!r}"
        )
    if target_status not in RADAR_ALLOWED_TRANSITIONS.get(from_status, set()):
        raise OptimizationError(f"forbidden radar transition {from_status}->{target_status}")
    updated = re.sub(
        r"^radarStatus:\s*.*$", f"radarStatus: {target_status}", text,
        count=1, flags=re.MULTILINE,
    )
    updated = re.sub(
        r"^radarStatusEffectiveAt:\s*.*$",
        f'radarStatusEffectiveAt: "{effective_at}"', updated,
        count=1, flags=re.MULTILINE,
    )
    match = RADAR_SECTION.search(updated)
    if not match:
        raise OptimizationError("missing Radar Status History section")
    body = match.group("body").rstrip()
    row = f"| {effective_at} | {target_status} | {actor} | {reason} |"
    replacement = match.group("head")[: len(match.group("head")) - len(match.group("body"))] + body + "\n" + row + "\n"
    return updated[:match.start()] + replacement + updated[match.end():]


def apply_ledger(
    ledger_path: Path, approval_path: Path, receipt_path: Path, write: bool,
) -> dict[str, Any]:
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    proposals = ledger.get("proposals") or []
    manifest = proposal_hash(proposals)
    if manifest != ledger.get("proposalManifestSha256"):
        raise OptimizationError("ledger proposal hash is invalid")
    if approval.get("proposalManifestSha256") != manifest:
        raise OptimizationError("approval is not bound to the exact proposal manifest")
    target_statuses = {item.get("proposedRadarStatus") for item in proposals}
    if target_statuses == {"shadow"}:
        expected_action = "move-to-shadow"
    elif target_statuses == {"disabled"}:
        expected_action = "move-to-disabled"
    else:
        raise OptimizationError("ledger must contain one supported target radar status")
    if approval.get("action") != expected_action:
        raise OptimizationError(f"approval action must be {expected_action}")
    actor = str(approval.get("approvedBy") or "").strip()
    effective_at = str(approval.get("approvedAt") or "").strip()
    if not actor or not effective_at:
        raise OptimizationError("approval requires approvedBy and approvedAt")
    dt.datetime.fromisoformat(effective_at.replace("Z", "+00:00"))
    records = {record.tag_id: record for record in load_registry()}
    changes: dict[Path, str] = {}
    for item in proposals:
        record = records.get(item["tagId"])
        if record is None:
            raise OptimizationError(f"proposal Tag entity is missing: {item['tagId']}")
        if record.radar_status != item["fromRadarStatus"]:
            raise OptimizationError(f"Tag changed after proposal: {record.tag_id}")
        reason = (
            f"Approved {item['proposedRadarStatus']} transition; evidence ledger "
            f"{ledger_path.as_posix()} manifest {manifest}."
        )
        changes[record.path] = transition_text(
            record.path.read_text(encoding="utf-8"), record.radar_status,
            item["proposedRadarStatus"], effective_at, actor, reason,
        )
    if write:
        for path, content in changes.items():
            path.write_text(content, encoding="utf-8")
        log = TAG_ROOT / "log.md"
        lines = [
            f"- {effective_at} | source: `{ledger_path.as_posix()}` ({manifest}) | "
            f"entity: [[{item['tagId']}]] | action: radarStatus "
            f"{item['fromRadarStatus']} -> {item['proposedRadarStatus']} | "
            "reasoning: approved evidence-backed radar optimization; article assignments unchanged."
            for item in proposals
        ]
        log.write_text(log.read_text(encoding="utf-8").rstrip() + "\n" + "\n".join(lines) + "\n", encoding="utf-8")
    payload = {
        "status": "applied" if write else "verified-preview",
        "proposalManifestSha256": manifest,
        "changedCount": len(changes),
        "approvedBy": actor,
        "approvedAt": effective_at,
        "databaseWrites": 0,
        "productionWrites": 0,
    }
    if write:
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze_parser = subparsers.add_parser("analyze")
    analyze_parser.add_argument("--target-percent", type=float, default=20.0)
    analyze_parser.add_argument("--radar-artifact", type=Path, action="append", default=[])
    analyze_parser.add_argument("--output", type=Path, required=True)
    analyze_parser.add_argument("--markdown-output", type=Path, required=True)
    apply_parser = subparsers.add_parser("apply-shadow")
    apply_parser.add_argument("--ledger", type=Path, required=True)
    apply_parser.add_argument("--approval", type=Path, required=True)
    apply_parser.add_argument("--receipt", type=Path, required=True)
    apply_parser.add_argument("--write", action="store_true")
    disable_parser = subparsers.add_parser("prepare-disable")
    disable_parser.add_argument("--shadow-ledger", type=Path, required=True)
    disable_parser.add_argument("--verification", type=Path, required=True)
    disable_parser.add_argument("--output", type=Path, required=True)
    disable_parser.add_argument("--markdown-output", type=Path, required=True)
    apply_disabled_parser = subparsers.add_parser("apply-disabled")
    apply_disabled_parser.add_argument("--ledger", type=Path, required=True)
    apply_disabled_parser.add_argument("--approval", type=Path, required=True)
    apply_disabled_parser.add_argument("--receipt", type=Path, required=True)
    apply_disabled_parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "analyze":
            if not 20.0 <= args.target_percent <= 30.0:
                raise OptimizationError("target percent must be between 20 and 30")
            payload = analyze(args.target_percent, [path.resolve() for path in args.radar_artifact])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(markdown_report(payload), encoding="utf-8")
            summary = {key: payload[key] for key in ("tagCount", "targetCount", "proposalCount", "protectedCount", "proposalManifestSha256", "databaseWrites", "productionWrites")}
        elif args.command in {"apply-shadow", "apply-disabled"}:
            summary = apply_ledger(
                args.ledger.resolve(), args.approval.resolve(), args.receipt.resolve(), args.write,
            )
        else:
            payload = prepare_disable(
                args.shadow_ledger.resolve(), args.verification.resolve(),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(
                "# Tag Radar Disable Proposal\n\n"
                f"- Proposed disabled tags: {payload['proposalCount']}\n"
                f"- Manifest SHA-256: `{payload['proposalManifestSha256']}`\n"
                f"- Shadow verification SHA-256: `{payload['shadowVerificationSha256']}`\n"
                "- Status: awaiting exact attributed approval\n",
                encoding="utf-8",
            )
            summary = {key: payload[key] for key in (
                "proposalCount", "proposalManifestSha256", "shadowVerificationSha256",
                "databaseWrites", "productionWrites",
            )}
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError, OptimizationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
