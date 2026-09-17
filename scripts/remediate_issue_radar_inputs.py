#!/usr/bin/env python3
"""Manifest-scoped remediation for compiled Issue Radar inputs.

The tool never invents judgments and never accesses a database. ``scan`` freezes
the current Markdown corpus and emits a local evidence pack plus mechanical
repairs and reviewed-exception candidates. A reviewer completes the decision
ledger. ``verify-decisions`` proves complete coverage of the frozen defects;
``apply`` preflights every note hash, backs up originals, and applies only the
accepted ledger; ``check`` proves that mandatory fields and exception coverage
are complete after the write.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from project_wiki_to_uat import scan_wiki
from article_quality import split_note
from ingest_cascade import split_database_projection
from wiki_uat_projection import ROOT, canonical_json, record_hash, render_projection
from tag_registry import load_registry as load_tag_registry


SCHEMA = "issue-radar-input-remediation.v1"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}
MANDATORY = {"issueTags", "category", "toneSentiment"}
EXCEPTION_FIELDS = {"coverage.country", "coverage.mediaOutletCategory"}
STOP_TAGS = {
    "animation-industry", "studio-sector", "animation", "animation", "security", "southeast-asia", "industry",
    "exercise", "training", "army", "navy", "air force", "animation-studio", "distribution-network", "ns",
    "animation", "character", "studio", "industry", "content", "series", "film",
    "china", "us", "usa", "united states", "war", "conflict", "sea", "camp",
}
ISSUE_TAG_HEADING = "## Issue Tags"


class RemediationError(RuntimeError):
    pass


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def section(body: str, heading: str) -> str:
    match = re.search(rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", body)
    return match.group(1).strip() if match else ""


def excerpts(note: dict[str, Any]) -> dict[str, Any]:
    body = note["body"]
    h1 = re.search(r"(?m)^# (.+)$", body)
    related = section(body, "Related Entities")
    return {
        "title": (h1.group(1).strip() if h1 else note["title"]),
        "summary": section(body, "Summary")[:1800],
        "keyPoints": section(body, "Key Points")[:2400],
        "sourceText": section(body, "Source Text")[:2400],
        "relatedEntities": related[:1600],
        "projectionTopic": (note["projection"].get("article") or {}).get("topic"),
    }


def load_inventory() -> tuple[set[str], str]:
    records = [record for record in load_tag_registry() if record.status == "active"]
    snapshot = canonical_json([
        {"tagId": record.tag_id, "displayName": record.display_name, "status": record.status}
        for record in records
    ])
    return {record.display_name.casefold() for record in records}, sha256_text(snapshot)


def outlet_registry() -> dict[str, list[dict[str, Any]]]:
    registry: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted((ROOT / "entities" / "outlet").glob("*.md")):
        if path.name in SYSTEM_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        match = re.match(r"(?s)^---\n(.*?)\n---", text)
        if not match:
            continue
        data = yaml.safe_load(match.group(1)) or {}
        row = {
            "path": path.relative_to(ROOT).as_posix(),
            "displayName": str(data.get("displayName") or "").strip(),
            "country": str(data.get("country") or "").strip() or None,
            "mediaCategory": str(data.get("mediaCategory") or "").strip() or None,
        }
        keys = {row["displayName"], str(data.get("outletId") or "").strip(), path.stem}
        keys.update(str(x).strip() for x in (data.get("aliases") or []))
        for key in keys:
            if key:
                registry[key.casefold()].append(row)
    return dict(registry)


def unique_outlet(registry: dict[str, list[dict[str, Any]]], name: str) -> dict[str, Any] | None:
    rows = registry.get(str(name or "").strip().casefold(), [])
    unique = {canonical_json(row): row for row in rows}
    return next(iter(unique.values())) if len(unique) == 1 else None


def defect_key(source_id: str, field: str, index: int | None = None) -> str:
    return f"{source_id}|{field}|{'' if index is None else index}"


def scan(args: argparse.Namespace) -> int:
    notes = scan_wiki(expected_count=None)
    inventory, inventory_hash = load_inventory()
    outlets = outlet_registry()
    manifest_rows = []
    defects = []
    evidence = []
    suggested = []
    publication_dates = []
    for source_id, note in sorted(notes.items()):
        projection = note.get("projection")
        if not projection:
            raise RemediationError(f"compiled note lacks projection: {note['relativePath']}")
        manifest_rows.append({
            "sourceId": source_id,
            "relativePath": note["relativePath"],
            "noteSha256": note["textSha256"],
            "projectionSha256": record_hash(projection),
        })
        article = projection.get("article") or {}
        published = str(article.get("published_date") or "")[:10]
        if published:
            publication_dates.append(published)
        common = {
            "sourceId": source_id,
            "relativePath": note["relativePath"],
            "noteSha256": note["textSha256"],
            "projectionSha256": record_hash(projection),
        }
        if not [x for x in projection.get("tags", []) if str(x).strip()]:
            row = {**common, "field": "issueTags", "index": None, "oldValue": []}
            defects.append(row)
            evidence.append({**row, "evidence": excerpts(note)})
        if article.get("category") in (None, ""):
            row = {**common, "field": "category", "index": None, "oldValue": article.get("category")}
            defects.append(row)
            evidence.append({**row, "evidence": excerpts(note)})
        if article.get("tone_sentiment") in (None, ""):
            row = {**common, "field": "toneSentiment", "index": None, "oldValue": article.get("tone_sentiment")}
            defects.append(row)
            local_value = str(note["metadata"].get("toneSentiment") or "").strip()
            evidence.append({**row, "evidence": {"frontmatterToneSentiment": local_value, **excerpts(note)}})
            if local_value in {"Positive", "Neutral", "Negative"}:
                suggested.append({**row, "newValue": local_value, "disposition": "repair",
                    "evidence": [f"{note['relativePath']}: frontmatter toneSentiment={local_value}"],
                    "confidence": 1.0, "rationale": "Exact mechanical synchronization from valid compiled frontmatter."})
        for index, coverage in enumerate(projection.get("coverage", [])):
            outlet = unique_outlet(outlets, coverage.get("display_name"))
            for field, key, outlet_key in (
                ("coverage.country", "country", "country"),
                ("coverage.mediaOutletCategory", "media_outlet_category", "mediaCategory"),
            ):
                if coverage.get(key) not in (None, ""):
                    continue
                row = {**common, "field": field, "index": index, "oldValue": coverage.get(key),
                       "coverageDisplayName": coverage.get("display_name")}
                defects.append(row)
                local_value = outlet.get(outlet_key) if outlet else None
                evidence.append({**row, "evidence": {
                    "outletMatch": outlet,
                    "coverageUrl": coverage.get("url"),
                }})
                if local_value:
                    suggested.append({**row, "newValue": local_value, "disposition": "repair",
                        "evidence": [f"{outlet['path']}: {outlet_key}={local_value}"], "confidence": 1.0,
                        "rationale": "Unique exact canonical outlet metadata match."})
                else:
                    suggested.append({**row, "newValue": None, "disposition": "reviewed-unavailable",
                        "evidence": [f"{note['relativePath']}: coverage row {index} names {coverage.get('display_name')!r}",
                                     "No unique canonical outlet value is present in the local outlet registry."],
                        "confidence": 1.0,
                        "rationale": "Local saved evidence does not establish this optional coverage field; no inference applied.",
                        "radarImpact": "Breadth/institutional attachment may be understated for this coverage row."})
    manifest_core = {
        "schemaVersion": SCHEMA,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "articleCount": len(manifest_rows),
        "publicationDateRange": [min(publication_dates), max(publication_dates)] if publication_dates else [],
        "tagRegistryPath": "entities/tag",
        "tagInventorySha256": inventory_hash,
        "tagInventoryCount": len(inventory),
        "articles": manifest_rows,
    }
    manifest_hash = record_hash(manifest_core)
    manifest = {**manifest_core, "manifestSha256": manifest_hash}
    for row in defects + evidence + suggested:
        row["frozenManifestSha256"] = manifest_hash
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "frozen-manifest.json", manifest)
    write_json(output / "defects.json", {"schemaVersion": SCHEMA, "defects": defects})
    write_json(output / "local-evidence-pack.json", {"schemaVersion": SCHEMA, "items": evidence})
    write_json(output / "decision-ledger.template.json", {
        "schemaVersion": SCHEMA,
        "frozenManifestSha256": manifest_hash,
        "reviewer": args.reviewer,
        "tagInventorySha256": inventory_hash,
        "decisions": suggested,
    })
    write_json(output / "scan-summary.json", {
        "status": "REVIEW_REQUIRED" if defects else "PASS", "articleCount": len(notes),
        "defectCount": len(defects), "countsByField": dict(sorted(
            (field, sum(1 for row in defects if row["field"] == field))
            for field in {row["field"] for row in defects}
        )), "manifestSha256": manifest_hash,
    })
    print(json.dumps({"status": "PASS", "outputDir": str(output), "defects": len(defects), "manifestSha256": manifest_hash}))
    return 0


def validate_decisions(bundle: Path, ledger_path: Path) -> dict[str, Any]:
    manifest = load_json(bundle / "frozen-manifest.json")
    defects = load_json(bundle / "defects.json")["defects"]
    ledger = load_json(ledger_path)
    if ledger.get("schemaVersion") != SCHEMA:
        raise RemediationError("decision ledger schema differs")
    if ledger.get("frozenManifestSha256") != manifest.get("manifestSha256"):
        raise RemediationError("decision ledger is not bound to the frozen manifest")
    reviewer = str(ledger.get("reviewer") or "").strip()
    if not reviewer:
        raise RemediationError("decision ledger has no attributed reviewer")
    decisions = ledger.get("decisions") or []
    by_key: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        key = defect_key(str(decision.get("sourceId")), str(decision.get("field")), decision.get("index"))
        if key in by_key:
            raise RemediationError(f"duplicate decision: {key}")
        by_key[key] = decision
    expected = {defect_key(row["sourceId"], row["field"], row.get("index")): row for row in defects}
    missing = sorted(set(expected) - set(by_key))
    extra = sorted(set(by_key) - set(expected))
    if missing or extra:
        raise RemediationError(f"decision coverage differs; missing={missing[:5]}, extra={extra[:5]}")
    inventory, inventory_hash = load_inventory()
    if inventory_hash != manifest["tagInventorySha256"]:
        raise RemediationError("tag inventory hash drifted")
    for key, decision in by_key.items():
        defect = expected[key]
        for field in ("sourceId", "relativePath", "noteSha256", "projectionSha256", "field"):
            if decision.get(field) != defect.get(field):
                raise RemediationError(f"decision does not match frozen defect {key}: {field}")
        if decision.get("index") != defect.get("index"):
            raise RemediationError(f"decision index does not match frozen defect {key}")
        disposition = decision.get("disposition")
        if disposition not in {"repair", "reviewed-unavailable"}:
            raise RemediationError(f"unresolved decision: {key}")
        if not decision.get("evidence") or not str(decision.get("rationale") or "").strip():
            raise RemediationError(f"decision lacks evidence/rationale: {key}")
        if float(decision.get("confidence") or 0) < 0.82:
            raise RemediationError(f"decision confidence below 0.82: {key}")
        field = decision["field"]
        if disposition == "reviewed-unavailable":
            if field not in EXCEPTION_FIELDS or not decision.get("radarImpact"):
                raise RemediationError(f"invalid reviewed exception: {key}")
            continue
        if field == "issueTags":
            tags = [str(x).strip() for x in decision.get("newValue") or [] if str(x).strip()]
            if not tags or any(tag.casefold() not in inventory for tag in tags):
                raise RemediationError(f"issue tag is empty or outside frozen inventory: {key}")
            if not any(tag.casefold() not in STOP_TAGS for tag in tags):
                raise RemediationError(f"issue tags are stop-list only: {key}")
            verification = decision.get("independentVerification") or {}
            if verification.get("accepted") is not True or not verification.get("reviewer") or not verification.get("evidence"):
                raise RemediationError(f"issue tags lack independent verification: {key}")
        elif field in MANDATORY and decision.get("newValue") in (None, "", []):
            raise RemediationError(f"mandatory field remains empty: {key}")
    return {"manifest": manifest, "defects": defects, "ledger": ledger, "decisions": by_key}


def verify_decisions(args: argparse.Namespace) -> int:
    result = validate_decisions(args.bundle_dir, args.decisions)
    report = {"status": "PASS", "manifestSha256": result["manifest"]["manifestSha256"],
              "decisionCount": len(result["decisions"]), "reviewer": result["ledger"]["reviewer"],
              "decisionLedgerSha256": sha256_text(args.decisions.read_text(encoding="utf-8"))}
    write_json(args.output, report)
    print(json.dumps(report))
    return 0


def replace_issue_tags(body: str, tags: list[str]) -> str:
    by_display = {
        record.display_name: record for record in load_tag_registry() if record.status == "active"
    }
    unknown = sorted(set(tags) - set(by_display))
    if unknown:
        raise RemediationError(f"issue tags have no active Tag entity: {unknown}")
    rendered = ISSUE_TAG_HEADING + "\n" + "\n".join(
        f"- [[tag/{by_display[tag].tag_id}|{tag}]]" for tag in tags
    ) + "\n\n"
    pattern = re.compile(r"(?ms)^## Issue Tags\s*\n.*?(?=^## |\Z)")
    if pattern.search(body):
        return pattern.sub(rendered, body, count=1).strip()
    anchors = ["## AI Context", "## Topic Consolidation Audit", "## Database Projection"]
    positions = [body.find(anchor) for anchor in anchors if body.find(anchor) >= 0]
    if positions:
        pos = min(positions)
        return (body[:pos].rstrip() + "\n\n" + rendered + body[pos:].lstrip()).strip()
    return (body.rstrip() + "\n\n" + rendered.rstrip()).strip()


def render_note(note: dict[str, Any], decisions: list[dict[str, Any]], provenance: str) -> str:
    projection = json.loads(canonical_json(note["projection"]))
    body = note["body"]
    tags = None
    for decision in decisions:
        field = decision["field"]
        if decision["disposition"] == "reviewed-unavailable":
            continue
        value = decision.get("newValue")
        if field == "issueTags":
            tags = list(value)
            projection["tags"] = tags
        elif field == "category":
            projection["article"]["category"] = value
        elif field == "toneSentiment":
            projection["article"]["tone_sentiment"] = value
        elif field == "coverage.country":
            projection["coverage"][int(decision["index"])]["country"] = value
        elif field == "coverage.mediaOutletCategory":
            projection["coverage"][int(decision["index"])]["media_outlet_category"] = value
    projection["reviewProvenance"] = provenance
    if tags is not None:
        body = replace_issue_tags(body, tags)
    prefix = note["text"][: note["text"].find(note["body"])]
    return prefix + body.rstrip() + "\n\n" + render_projection(projection)


def apply(args: argparse.Namespace) -> int:
    validated = validate_decisions(args.bundle_dir, args.decisions)
    manifest = validated["manifest"]
    notes = scan_wiki(expected_count=None)
    frozen = {row["sourceId"]: row for row in manifest["articles"]}
    if set(notes) != set(frozen):
        raise RemediationError("corpus membership drifted from frozen manifest")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in validated["ledger"]["decisions"]:
        grouped[str(decision["sourceId"])].append(decision)
    provenance = args.decisions.resolve().relative_to(ROOT).as_posix()
    rendered = {}
    already_applied = 0
    for source_id, row in frozen.items():
        note = notes[source_id]
        if note["textSha256"] == row["noteSha256"] and record_hash(note["projection"]) == row["projectionSha256"]:
            if source_id in grouped:
                rendered[source_id] = render_note(note, grouped[source_id], provenance)
            continue
        if source_id in grouped:
            expected_current = render_note(note, grouped[source_id], provenance)
            if sha256_text(expected_current) == note["textSha256"]:
                already_applied += 1
                rendered[source_id] = expected_current
                continue
        raise RemediationError(f"frozen note hash drifted unexpectedly: {source_id}")
    if not args.write:
        would_write = sum(
            sha256_text(text) != notes[source_id]["textSha256"]
            for source_id, text in rendered.items()
        )
        report = {"status": "DRY_RUN", "scope": len(rendered), "wouldWrite": would_write,
                  "idempotent": would_write == 0, "manifestSha256": manifest["manifestSha256"]}
        write_json(args.report, report)
        print(json.dumps(report))
        return 0
    args.backup.parent.mkdir(parents=True, exist_ok=True)
    if not args.backup.exists():
        with gzip.open(args.backup, "wt", encoding="utf-8") as handle:
            for source_id in sorted(rendered):
                note = notes[source_id]
                handle.write(canonical_json({"relativePath": note["relativePath"], "noteSha256": note["textSha256"], "text": note["text"]}) + "\n")
    else:
        with gzip.open(args.backup, "rt", encoding="utf-8") as handle:
            backup_rows = [json.loads(line) for line in handle if line.strip()]
        if len(backup_rows) != len(rendered) or any(sha256_text(row["text"]) != row["noteSha256"] for row in backup_rows):
            raise RemediationError("existing backup is incomplete or invalid")
    replaced: list[tuple[Path, str]] = []
    try:
        for source_id in sorted(rendered):
            if sha256_text(rendered[source_id]) == notes[source_id]["textSha256"]:
                continue
            path = notes[source_id]["path"]
            original = notes[source_id]["text"]
            fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(rendered[source_id])
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            replaced.append((path, original))
    except Exception:
        for path, original in reversed(replaced):
            path.write_text(original, encoding="utf-8")
        raise
    report = {"status": "PASS", "changedScope": len(rendered), "alreadyAppliedAtResume": already_applied,
              "writesThisInvocation": len(replaced), "backup": args.backup.resolve().relative_to(ROOT).as_posix(),
              "manifestSha256": manifest["manifestSha256"], "decisionLedgerSha256": sha256_text(args.decisions.read_text(encoding="utf-8")),
              "resultingNoteHashes": {source_id: sha256_text(rendered[source_id]) for source_id in sorted(rendered)}}
    write_json(args.report, report)
    print(json.dumps({"status": "PASS", "changed": len(rendered)}))
    return 0


def check(args: argparse.Namespace) -> int:
    validated = validate_decisions(args.bundle_dir, args.decisions)
    notes = scan_wiki(expected_count=None)
    unresolved = []
    exception_count = 0
    for decision in validated["ledger"]["decisions"]:
        note = notes[str(decision["sourceId"])]
        projection = note["projection"]
        field = decision["field"]
        if decision["disposition"] == "reviewed-unavailable":
            exception_count += 1
            if projection.get("reviewProvenance") != args.decisions.resolve().relative_to(ROOT).as_posix():
                unresolved.append(defect_key(decision["sourceId"], field, decision.get("index")))
            continue
        expected = decision["newValue"]
        if field == "issueTags": observed = projection.get("tags")
        elif field == "category": observed = projection["article"].get("category")
        elif field == "toneSentiment": observed = projection["article"].get("tone_sentiment")
        elif field == "coverage.country": observed = projection["coverage"][int(decision["index"])].get("country")
        else: observed = projection["coverage"][int(decision["index"])].get("media_outlet_category")
        if observed != expected:
            unresolved.append(defect_key(decision["sourceId"], field, decision.get("index")))
    report = {"status": "PASS" if not unresolved else "FAIL", "articleCount": len(notes),
              "unresolved": unresolved, "reviewedExceptionCount": exception_count,
              "decisionCount": len(validated["decisions"])}
    write_json(args.output, report)
    print(json.dumps(report))
    if unresolved:
        raise RemediationError(f"post-remediation check found {len(unresolved)} unresolved decisions")
    return 0


def restore(args: argparse.Namespace) -> int:
    """Restore an interrupted apply from its exact compressed originals."""

    validated = validate_decisions(args.bundle_dir, args.decisions)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in validated["ledger"]["decisions"]:
        grouped[str(decision["sourceId"])].append(decision)
    provenance = args.decisions.resolve().relative_to(ROOT).as_posix()
    backups = []
    with gzip.open(args.backup, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                backups.append(json.loads(line))
    if len(backups) != len(grouped):
        raise RemediationError(
            f"backup scope differs: expected {len(grouped)}, found {len(backups)}"
        )
    pending = []
    for row in backups:
        path = ROOT / row["relativePath"]
        original = row["text"]
        if sha256_text(original) != row["noteSha256"]:
            raise RemediationError(f"backup original hash differs: {row['relativePath']}")
        frontmatter_raw, body_with_delimiter = split_note(original)
        metadata = yaml.safe_load(frontmatter_raw) or {}
        source_body, projection_raw = split_database_projection(body_with_delimiter.lstrip("\n"))
        projection = json.loads(projection_raw or "null")
        source_id = str((projection.get("identity") or {}).get("wikiSourceId") or metadata.get("sourceId") or "")
        if source_id not in grouped:
            raise RemediationError(f"backup source is outside decision scope: {source_id}")
        note = {"text": original, "body": source_body, "projection": projection}
        expected = render_note(note, grouped[source_id], provenance)
        current = path.read_text(encoding="utf-8")
        current_hash = sha256_text(current)
        if current_hash not in {row["noteSha256"], sha256_text(expected)}:
            raise RemediationError(f"cannot restore unexpectedly changed note: {row['relativePath']}")
        if current_hash != row["noteSha256"]:
            pending.append((path, original))
    for path, original in pending:
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".restore.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(original)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    report = {"status": "PASS", "restored": len(pending), "backupRows": len(backups)}
    write_json(args.report, report)
    print(json.dumps(report))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    sub = value.add_subparsers(dest="command", required=True)
    scan_p = sub.add_parser("scan")
    scan_p.add_argument("--output-dir", type=Path, required=True)
    scan_p.add_argument("--reviewer", required=True)
    scan_p.set_defaults(func=scan)
    for name, func in (("verify-decisions", verify_decisions), ("apply", apply), ("check", check), ("restore", restore)):
        item = sub.add_parser(name)
        item.add_argument("--bundle-dir", type=Path, required=True)
        item.add_argument("--decisions", type=Path, required=True)
        if name == "verify-decisions":
            item.add_argument("--output", type=Path, required=True)
        elif name == "apply":
            item.add_argument("--write", action="store_true")
            item.add_argument("--backup", type=Path, required=True)
            item.add_argument("--report", type=Path, required=True)
        elif name == "restore":
            item.add_argument("--backup", type=Path, required=True)
            item.add_argument("--report", type=Path, required=True)
        else:
            item.add_argument("--output", type=Path, required=True)
        item.set_defaults(func=func)
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args)
    except (RemediationError, ValueError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"ERROR: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
