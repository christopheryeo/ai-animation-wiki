#!/usr/bin/env python3
"""Create canonical run receipts under runs/.

The ai-animation vault uses Markdown notes as the source of truth, but
operational throughput needs a structured receipt per run. This module provides
both:

1. An importable RunLogger for scripts that can time stages directly.
2. A small CLI for recording measured counts/durations from ad hoc runs.

Canonical receipts are written to:
  runs/YYYY-MM-DD/YYYYMMDDTHHMMSS-<operation>-<short-id>.json

Temporary inputs, previews, and other non-receipt JSON should live under:
  runs/YYYY-MM-DD/artifacts/
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional


ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
SCHEMA_VERSION = "run-log.v1"
OPERATIONS = {
    "raw_to_inputs",
    "ingest",
    "cascade",
    "ingest_cascade",
    "lint",
    "query",
    "raw_to_inputs_ingest_cascade",
}
STATUSES = {"ok", "partial", "failed", "aborted"}


def now_dt() -> datetime:
    """Return timezone-aware local time."""
    return datetime.now().astimezone()


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return slug or "run"


def elapsed_seconds(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds(), 3)


def average(duration_sec: Optional[float], count: Optional[int]) -> Optional[float]:
    if duration_sec is None or not count:
        return None
    return round(float(duration_sec) / int(count), 6)


def default_article_metrics() -> Dict[str, Any]:
    return {
        "inputCount": 0,
        "processedCount": 0,
        "createdCount": 0,
        "updatedCount": 0,
        "skippedCount": 0,
        "duplicateCount": 0,
        "failedCount": 0,
        "avgSecPerArticle": None,
    }


def default_file_metrics() -> Dict[str, Any]:
    return {
        "scannedCount": 0,
        "changedCount": 0,
        "failedCount": 0,
        "avgSecPerFile": None,
    }


def make_run_id(operation: str, started_at: datetime, suffix: Optional[str] = None) -> str:
    short_id = suffix or uuid.uuid4().hex[:4]
    stamp = started_at.strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{slugify(operation)}-{short_id}"


def receipt_path(run_id: str, started_at: datetime, runs_dir: Path = RUNS_DIR) -> Path:
    return runs_dir / started_at.strftime("%Y-%m-%d") / f"{run_id}.json"


def artifacts_dir(started_at: datetime, runs_dir: Path = RUNS_DIR) -> Path:
    return runs_dir / started_at.strftime("%Y-%m-%d") / "artifacts"


def build_receipt(
    operation: str,
    status: str,
    trigger: str,
    started_at: datetime,
    ended_at: datetime,
    run_id: Optional[str] = None,
    article_metrics: Optional[Dict[str, Any]] = None,
    file_metrics: Optional[Dict[str, Any]] = None,
    stage_metrics: Optional[List[Dict[str, Any]]] = None,
    outputs: Optional[Dict[str, Any]] = None,
    errors: Optional[List[Dict[str, Any]]] = None,
    notes: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if operation not in OPERATIONS:
        raise ValueError(f"Unsupported operation {operation!r}; expected one of {sorted(OPERATIONS)}")
    if status not in STATUSES:
        raise ValueError(f"Unsupported status {status!r}; expected one of {sorted(STATUSES)}")

    duration_sec = elapsed_seconds(started_at, ended_at)
    article = default_article_metrics()
    if article_metrics:
        article.update({k: v for k, v in article_metrics.items() if v is not None})
    if article.get("avgSecPerArticle") is None:
        article["avgSecPerArticle"] = average(duration_sec, article.get("processedCount"))

    files = default_file_metrics()
    if file_metrics:
        files.update({k: v for k, v in file_metrics.items() if v is not None})
    if files.get("avgSecPerFile") is None:
        files["avgSecPerFile"] = average(duration_sec, files.get("scannedCount"))

    rid = run_id or make_run_id(operation, started_at)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "runId": rid,
        "operation": operation,
        "status": status,
        "trigger": trigger,
        "startedAt": iso(started_at),
        "endedAt": iso(ended_at),
        "durationSec": duration_sec,
        "articleMetrics": article,
        "fileMetrics": files,
        "stageMetrics": stage_metrics or [],
        "outputs": outputs or {},
        "errors": errors or [],
        "notes": notes,
        "metadata": metadata or {},
    }


def write_receipt(receipt: Dict[str, Any], runs_dir: Path = RUNS_DIR) -> Path:
    started_at = parse_dt(receipt["startedAt"])
    if started_at is None:
        raise ValueError("receipt missing startedAt")
    path = receipt_path(receipt["runId"], started_at, runs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(receipt, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path


def validate_receipt(receipt: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    required = [
        "schemaVersion",
        "runId",
        "operation",
        "status",
        "trigger",
        "startedAt",
        "endedAt",
        "durationSec",
        "articleMetrics",
        "stageMetrics",
    ]
    for key in required:
        if key not in receipt:
            errors.append(f"missing required field: {key}")
    if receipt.get("schemaVersion") != SCHEMA_VERSION:
        errors.append(f"schemaVersion must be {SCHEMA_VERSION!r}")
    if receipt.get("operation") not in OPERATIONS:
        errors.append(f"operation must be one of {sorted(OPERATIONS)}")
    if receipt.get("status") not in STATUSES:
        errors.append(f"status must be one of {sorted(STATUSES)}")
    for dt_key in ("startedAt", "endedAt"):
        try:
            parse_dt(receipt.get(dt_key))
        except Exception as exc:  # noqa: BLE001 - validation should report all field errors.
            errors.append(f"{dt_key} is not ISO datetime: {exc}")
    article = receipt.get("articleMetrics", {})
    for key in (
        "inputCount",
        "processedCount",
        "createdCount",
        "updatedCount",
        "skippedCount",
        "duplicateCount",
        "failedCount",
        "avgSecPerArticle",
    ):
        if key not in article:
            errors.append(f"articleMetrics missing field: {key}")
    for i, stage in enumerate(receipt.get("stageMetrics", []), start=1):
        for key in ("stage", "startedAt", "endedAt", "durationSec", "status"):
            if key not in stage:
                errors.append(f"stageMetrics[{i}] missing field: {key}")
    return errors


class Stage:
    def __init__(self, name: str, status: str = "ok", article_count: int = 0, file_count: int = 0):
        self.name = name
        self.status = status
        self.article_count = article_count
        self.file_count = file_count
        self.started_at: Optional[datetime] = None
        self.ended_at: Optional[datetime] = None
        self.metrics: Dict[str, Any] = {}
        self.errors: List[Dict[str, Any]] = []

    def set_metric(self, key: str, value: Any) -> None:
        self.metrics[key] = value

    def add_error(self, message: str, **extra: Any) -> None:
        error = {"message": message}
        error.update(extra)
        self.errors.append(error)
        self.status = "failed"

    def to_dict(self) -> Dict[str, Any]:
        if self.started_at is None or self.ended_at is None:
            raise ValueError(f"stage {self.name!r} was not timed")
        duration_sec = elapsed_seconds(self.started_at, self.ended_at)
        data = {
            "stage": self.name,
            "startedAt": iso(self.started_at),
            "endedAt": iso(self.ended_at),
            "durationSec": duration_sec,
            "articleCount": self.article_count,
            "avgSecPerArticle": average(duration_sec, self.article_count),
            "fileCount": self.file_count,
            "avgSecPerFile": average(duration_sec, self.file_count),
            "status": self.status,
        }
        if self.metrics:
            data["metrics"] = deepcopy(self.metrics)
        if self.errors:
            data["errors"] = deepcopy(self.errors)
        return data


class RunLogger:
    """Context manager for scripts that need consistent run receipts."""

    def __init__(
        self,
        operation: str,
        trigger: str = "manual",
        runs_dir: Path = RUNS_DIR,
        run_id: Optional[str] = None,
        notes: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ):
        if operation not in OPERATIONS:
            raise ValueError(f"Unsupported operation {operation!r}")
        self.operation = operation
        self.trigger = trigger
        self.runs_dir = runs_dir
        self.started_at = now_dt()
        self.ended_at: Optional[datetime] = None
        self.run_id = run_id or make_run_id(operation, self.started_at)
        self.notes = notes
        self.metadata = metadata or {}
        self.status = "ok"
        self.article_metrics = default_article_metrics()
        self.file_metrics = default_file_metrics()
        self.stage_metrics: List[Dict[str, Any]] = []
        self.outputs: Dict[str, Any] = {}
        self.errors: List[Dict[str, Any]] = []
        self.path: Optional[Path] = None

    def __enter__(self) -> "RunLogger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        if exc is not None:
            self.status = "failed"
            self.errors.append({"message": str(exc), "type": getattr(exc_type, "__name__", "Exception")})
        self.finish()
        return False

    @contextmanager
    def stage(self, name: str, article_count: int = 0, file_count: int = 0) -> Iterator[Stage]:
        stage = Stage(name=name, article_count=article_count, file_count=file_count)
        stage.started_at = now_dt()
        try:
            yield stage
        except Exception as exc:
            stage.add_error(str(exc), type=type(exc).__name__)
            raise
        finally:
            stage.ended_at = now_dt()
            self.stage_metrics.append(stage.to_dict())

    def set_article_metrics(self, **metrics: Any) -> None:
        self.article_metrics.update({k: v for k, v in metrics.items() if v is not None})

    def set_file_metrics(self, **metrics: Any) -> None:
        self.file_metrics.update({k: v for k, v in metrics.items() if v is not None})

    def add_output(self, key: str, value: Any) -> None:
        self.outputs[key] = value

    def add_error(self, message: str, **extra: Any) -> None:
        error = {"message": message}
        error.update(extra)
        self.errors.append(error)
        self.status = "partial"

    def finish(self) -> Path:
        if self.ended_at is None:
            self.ended_at = now_dt()
        receipt = build_receipt(
            operation=self.operation,
            status=self.status,
            trigger=self.trigger,
            started_at=self.started_at,
            ended_at=self.ended_at,
            run_id=self.run_id,
            article_metrics=self.article_metrics,
            file_metrics=self.file_metrics,
            stage_metrics=self.stage_metrics,
            outputs=self.outputs,
            errors=self.errors,
            notes=self.notes,
            metadata=self.metadata,
        )
        self.path = write_receipt(receipt, self.runs_dir)
        return self.path


def parse_key_values(values: List[str]) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    for value in values:
        key, sep, raw = value.partition("=")
        if not sep:
            raise ValueError(f"Expected key=value, got {value!r}")
        raw = raw.strip()
        if raw.lower() in {"true", "false"}:
            parsed[key] = raw.lower() == "true"
        else:
            try:
                parsed[key] = int(raw)
            except ValueError:
                try:
                    parsed[key] = float(raw)
                except ValueError:
                    parsed[key] = raw
    return parsed


def build_record_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    started_at = parse_dt(args.started_at) or now_dt()
    if args.ended_at:
        ended_at = parse_dt(args.ended_at)
    elif args.duration_sec is not None:
        ended_at = datetime.fromtimestamp(started_at.timestamp() + args.duration_sec, tz=started_at.tzinfo)
    else:
        ended_at = now_dt()
    assert ended_at is not None

    article_metrics = {
        "inputCount": args.input_count,
        "processedCount": args.processed_count,
        "createdCount": args.created_count,
        "updatedCount": args.updated_count,
        "skippedCount": args.skipped_count,
        "duplicateCount": args.duplicate_count,
        "failedCount": args.failed_count,
    }
    file_metrics = {
        "scannedCount": args.files_scanned,
        "changedCount": args.files_changed,
        "failedCount": args.files_failed,
    }
    metadata = parse_key_values(args.metadata or [])
    return build_receipt(
        operation=args.operation,
        status=args.status,
        trigger=args.trigger,
        started_at=started_at,
        ended_at=ended_at,
        run_id=args.run_id,
        article_metrics=article_metrics,
        file_metrics=file_metrics,
        stage_metrics=[],
        outputs={},
        errors=[],
        notes=args.notes or "",
        metadata=metadata,
    )


def sample_receipt() -> Dict[str, Any]:
    started_at = now_dt()
    stage1_start = started_at
    stage1_end = datetime.fromtimestamp(stage1_start.timestamp() + 12.5, tz=stage1_start.tzinfo)
    stage2_start = stage1_end
    stage2_end = datetime.fromtimestamp(stage2_start.timestamp() + 27.5, tz=stage2_start.tzinfo)
    ended_at = stage2_end
    return build_receipt(
        operation="ingest_cascade",
        status="ok",
        trigger="manual",
        started_at=started_at,
        ended_at=ended_at,
        article_metrics={
            "inputCount": 10,
            "processedCount": 10,
            "createdCount": 10,
            "updatedCount": 0,
            "skippedCount": 0,
            "duplicateCount": 0,
            "failedCount": 0,
        },
        stage_metrics=[
            {
                "stage": "ingest",
                "startedAt": iso(stage1_start),
                "endedAt": iso(stage1_end),
                "durationSec": 12.5,
                "articleCount": 10,
                "avgSecPerArticle": 1.25,
                "fileCount": 10,
                "avgSecPerFile": 1.25,
                "status": "ok",
            },
            {
                "stage": "cascade",
                "startedAt": iso(stage2_start),
                "endedAt": iso(stage2_end),
                "durationSec": 27.5,
                "articleCount": 10,
                "avgSecPerArticle": 2.75,
                "fileCount": 0,
                "avgSecPerFile": None,
                "status": "ok",
                "metrics": {
                    "entityUpdates": {
                        "people": 4,
                        "organisations": 8,
                        "places": 3,
                        "outlets": 5,
                        "countries": 2,
                        "topics": 6,
                    }
                },
            },
        ],
        notes="Example receipt generated by scripts/run_logger.py --sample.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Create and validate canonical run receipts.")
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="Write a measured run receipt.")
    record.add_argument("--operation", required=True, choices=sorted(OPERATIONS))
    record.add_argument("--status", default="ok", choices=sorted(STATUSES))
    record.add_argument("--trigger", default="manual")
    record.add_argument("--started-at")
    record.add_argument("--ended-at")
    record.add_argument("--duration-sec", type=float)
    record.add_argument("--run-id")
    record.add_argument("--input-count", type=int, default=0)
    record.add_argument("--processed-count", type=int, default=0)
    record.add_argument("--created-count", type=int, default=0)
    record.add_argument("--updated-count", type=int, default=0)
    record.add_argument("--skipped-count", type=int, default=0)
    record.add_argument("--duplicate-count", type=int, default=0)
    record.add_argument("--failed-count", type=int, default=0)
    record.add_argument("--files-scanned", type=int, default=0)
    record.add_argument("--files-changed", type=int, default=0)
    record.add_argument("--files-failed", type=int, default=0)
    record.add_argument("--metadata", action="append", default=[], help="Additional key=value metadata.")
    record.add_argument("--notes", default="")
    record.add_argument("--dry-run", action="store_true", help="Print the receipt instead of writing it.")

    sample = sub.add_parser("sample", help="Print or write an example receipt.")
    sample.add_argument("--write", action="store_true", help="Write the sample under runs/YYYY-MM-DD/.")
    sample.add_argument("--output", help="Write the sample to a specific path instead of runs/.")

    validate = sub.add_parser("validate", help="Validate a run receipt JSON file.")
    validate.add_argument("path")

    args = parser.parse_args()

    if args.command == "record":
        receipt = build_record_from_args(args)
        problems = validate_receipt(receipt)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        if args.dry_run:
            print(json.dumps(receipt, indent=2, ensure_ascii=False))
            return 0
        path = write_receipt(receipt)
        print(path.relative_to(ROOT))
        return 0

    if args.command == "sample":
        receipt = sample_receipt()
        if args.output:
            path = Path(args.output)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(receipt, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(path)
            return 0
        if args.write:
            path = write_receipt(receipt)
            print(path.relative_to(ROOT))
            return 0
        print(json.dumps(receipt, indent=2, ensure_ascii=False))
        return 0

    if args.command == "validate":
        with open(args.path, encoding="utf-8") as f:
            receipt = json.load(f)
        problems = validate_receipt(receipt)
        if problems:
            for problem in problems:
                print(problem, file=sys.stderr)
            return 1
        print("ok")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
