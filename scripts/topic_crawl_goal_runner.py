#!/usr/bin/env python3
"""Persist, validate, and close autonomous Topic Crawl goals."""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_NAME, LEDGER_NAME, RECEIPT_NAME = "goal-state.json", "candidates.ndjson", "goal-receipt.json"
TERMINAL = {"cascaded", "duplicate", "off-topic", "rejected", "held"}
CRITICAL_KINDS = {"credential-unavailable", "credential-rejected", "required-discovery-unavailable",
                  "provider-systemic-failure", "provenance-conflict", "manifest-integrity-failure",
                  "unauthorized-write-required", "production-action-required", "unrecoverable-validation-failure"}
PHASES = {"identity", "date", "completeness", "relevance", "normalized", "enriched", "dryRun", "cascaded", "topicLinked", "validated"}


class GoalError(RuntimeError):
    pass


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoalError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GoalError(f"{path} must contain a JSON object")
    return value


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise GoalError(f"{path}:{line_no}: invalid JSON: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise GoalError(f"{path}:{line_no}: candidate must be an object")
        row["_line"] = line_no
        rows.append(row)
    return rows


def receipt_valid(path_value: Any, month: str, dry_run: bool) -> bool:
    if not isinstance(path_value, str) or not path_value.strip():
        return False
    try:
        value = load_json(ROOT / path_value)
    except GoalError:
        return False
    metrics, metadata = value.get("articleMetrics"), value.get("metadata")
    return (value.get("operation") == "ingest_cascade" and value.get("status") == "ok"
            and isinstance(metrics, dict) and isinstance(metadata, dict)
            and metadata.get("month") == month and metadata.get("dryRun") is dry_run
            and isinstance(metrics.get("processedCount"), int) and metrics["processedCount"] > 0
            and metrics.get("failedCount") == 0)


def coverage_has_article(topic_id: str, article_path: str) -> bool:
    path = ROOT / "entities" / "topic" / f"{topic_id}.md"
    if not path.exists():
        return False
    coverage = path.read_text(encoding="utf-8").partition("## Coverage\n")[2].partition("\n## ")[0]
    return Path(article_path).stem in coverage


def validate(state: dict[str, Any], rows: list[dict[str, Any]], _run_dir: Path) -> dict[str, Any]:
    errors, seen_ids, seen_keys = [], set(), set()
    counts = {status: 0 for status in sorted(TERMINAL)}
    topics, months = set(state.get("topics") or []), set()
    if not topics:
        errors.append("goal has no selected topics")
    for row in rows:
        line, candidate_id, topic_id = row["_line"], str(row.get("candidateId") or "").strip(), str(row.get("topicId") or "").strip()
        disposition, identity = str(row.get("disposition") or "").strip(), str(row.get("providerUri") or row.get("canonicalUrl") or candidate_id).strip()
        if not candidate_id or candidate_id in seen_ids:
            errors.append(f"line {line}: missing or duplicate candidateId")
        seen_ids.add(candidate_id)
        if topic_id not in topics:
            errors.append(f"line {line}: unknown topicId {topic_id!r}")
        if row.get("set") not in {"A", "B"}:
            errors.append(f"line {line}: set must be A or B")
        if disposition not in TERMINAL:
            errors.append(f"line {line}: disposition must be one of {sorted(TERMINAL)}")
            continue
        counts[disposition] += 1
        if not str(row.get("reason") or "").strip():
            errors.append(f"line {line}: missing evidence-backed reason")
        if identity in seen_keys and disposition != "duplicate":
            errors.append(f"line {line}: duplicate identity requires duplicate disposition")
        seen_keys.add(identity)
        if disposition == "duplicate" and not row.get("duplicateOf"):
            errors.append(f"line {line}: duplicate candidate requires duplicateOf")
        if disposition == "held" and not row.get("holdStage"):
            errors.append(f"line {line}: held candidate requires holdStage")
        if disposition == "cascaded":
            article_path, intake_path = str(row.get("articlePath") or ""), str(row.get("intakePath") or "")
            if not article_path or not intake_path or not (ROOT / article_path).exists():
                errors.append(f"line {line}: cascaded candidate requires existing intakePath and articlePath")
            else:
                parts = Path(article_path).parts
                try:
                    months.add(parts[parts.index("article") + 1])
                except (ValueError, IndexError):
                    errors.append(f"line {line}: articlePath must be under entities/article/YYYY-MM")
                if not coverage_has_article(topic_id, article_path):
                    errors.append(f"line {line}: target topic Coverage does not link articlePath")
            phases = row.get("phaseEvidence")
            if not isinstance(phases, dict) or any(not str(phases.get(name) or "").strip() for name in PHASES):
                errors.append(f"line {line}: cascaded candidate lacks complete phaseEvidence")
    for event in state.get("criticalEvents") or []:
        if not isinstance(event, dict) or event.get("resolved") is not True:
            errors.append(f"unresolved critical event: {event.get('kind') if isinstance(event, dict) else 'unclassified'}")
        elif event.get("kind") not in CRITICAL_KINDS:
            errors.append(f"unknown critical event kind: {event.get('kind')!r}")
    for topic in topics:
        checkpoint = (state.get("topicCompletion") or {}).get(topic)
        if not isinstance(checkpoint, dict) or checkpoint.get("status") != "completed" or not checkpoint.get("verifiedAt"):
            errors.append(f"topic {topic}: completion checkpoint is not recorded and verified")
    for month in months:
        entry = (state.get("monthReceipts") or {}).get(month)
        if not isinstance(entry, dict) or not receipt_valid(entry.get("dryRunReceipt"), month, True) or not receipt_valid(entry.get("cascadeReceipt"), month, False):
            errors.append(f"month {month}: missing or invalid dry-run/real cascade receipts")
    return {"valid": not errors, "candidateCount": len(rows), "terminalCounts": counts, "errors": errors, "state": state.get("status")}


def init(args: argparse.Namespace) -> int:
    run_dir = args.run_dir.resolve()
    run_dir.mkdir(parents=True, exist_ok=False)
    if args.date_start > args.date_end:
        raise GoalError("date-start must not be later than date-end")
    state = {"schemaVersion": "topic-crawl-goal.v1", "status": "running", "startedAt": now(), "updatedAt": now(),
             "topics": list(dict.fromkeys(args.topic)), "dateStart": args.date_start, "dateEnd": args.date_end,
             "timezone": args.timezone, "maxCandidatesPerSet": args.max_candidates, "batchSize": args.batch_size,
             "retryPolicy": {"maxAttempts": args.max_attempts, "backoffSeconds": args.backoff_seconds},
             "policy": "schemas/topic_crawl_resolution_policy.yaml", "criticalEvents": [], "topicCompletion": {}, "monthReceipts": {}}
    write_json(run_dir / STATE_NAME, state)
    (run_dir / LEDGER_NAME).write_text("", encoding="utf-8")
    print(json.dumps({"runDir": str(run_dir), "status": "running", "topics": state["topics"]}, indent=2))
    return 0


def record_month_receipts(args: argparse.Namespace) -> int:
    state = load_json(args.run_dir.resolve() / STATE_NAME)
    try:
        dry, real = str(args.dry_run_receipt.resolve().relative_to(ROOT)), str(args.cascade_receipt.resolve().relative_to(ROOT))
    except ValueError as exc:
        raise GoalError("receipt must be inside the vault") from exc
    if not receipt_valid(dry, args.month, True) or not receipt_valid(real, args.month, False):
        raise GoalError(f"invalid receipts for {args.month}")
    state.setdefault("monthReceipts", {})[args.month] = {"dryRunReceipt": dry, "cascadeReceipt": real, "recordedAt": now()}
    state["updatedAt"] = now()
    write_json(args.run_dir.resolve() / STATE_NAME, state)
    print(json.dumps({"month": args.month, "dryRunReceipt": dry, "cascadeReceipt": real}, indent=2))
    return 0


def reconcile(args: argparse.Namespace, close: bool) -> int:
    run_dir, state = args.run_dir.resolve(), load_json(args.run_dir.resolve() / STATE_NAME)
    result = validate(state, load_candidates(run_dir / LEDGER_NAME), run_dir)
    result["runDir"] = str(run_dir)
    if close and result["valid"]:
        state["status"], state["completedAt"], state["updatedAt"] = "completed", now(), now()
        receipt = {"schemaVersion": "topic-crawl-goal-receipt.v1", "status": "ok", "startedAt": state["startedAt"], "endedAt": state["completedAt"],
                   "parameters": {key: state[key] for key in ("topics", "dateStart", "dateEnd", "timezone", "maxCandidatesPerSet", "batchSize", "retryPolicy")},
                   "candidateMetrics": result["terminalCounts"], "candidateCount": result["candidateCount"], "monthReceipts": state.get("monthReceipts", {}),
                   "reconciliation": "exactly one terminal disposition per ledger record", "criticalEvents": state.get("criticalEvents", [])}
        write_json(run_dir / STATE_NAME, state); write_json(run_dir / RECEIPT_NAME, receipt); result["closed"] = True
    elif close:
        result["closed"] = False
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__); commands = parser.add_subparsers(dest="command", required=True)
    init_parser = commands.add_parser("init"); init_parser.add_argument("--run-dir", type=Path, required=True); init_parser.add_argument("--topic", action="append", required=True); init_parser.add_argument("--date-start", required=True); init_parser.add_argument("--date-end", required=True); init_parser.add_argument("--timezone", default="Asia/Singapore"); init_parser.add_argument("--max-candidates", type=int, default=500); init_parser.add_argument("--batch-size", type=int, default=10); init_parser.add_argument("--max-attempts", type=int, default=3); init_parser.add_argument("--backoff-seconds", type=int, default=20)
    for name in ("reconcile", "close"):
        command = commands.add_parser(name); command.add_argument("--run-dir", type=Path, required=True)
    receipts = commands.add_parser("record-month-receipts"); receipts.add_argument("--run-dir", type=Path, required=True); receipts.add_argument("--month", required=True); receipts.add_argument("--dry-run-receipt", type=Path, required=True); receipts.add_argument("--cascade-receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        return init(args) if args.command == "init" else record_month_receipts(args) if args.command == "record-month-receipts" else reconcile(args, args.command == "close")
    except GoalError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
