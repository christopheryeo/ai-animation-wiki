#!/usr/bin/env python3
"""Prove every UAT update in a remediation bundle maps to an accepted repair."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from project_wiki_to_uat import load_projection_records, verify_bundle_dir


def locator(field: str, index: int | None = None) -> str:
    return field if index is None else f"{field}[{index}]"


def changed_fields(old: dict[str, Any], new: dict[str, Any]) -> set[str]:
    changed = set()
    if old["article"].get("category") != new["article"].get("category"):
        changed.add("category")
    if old["article"].get("tone_sentiment") != new["article"].get("tone_sentiment"):
        changed.add("toneSentiment")
    if old.get("tags") != new.get("tags"):
        changed.add("issueTags")
    old_coverage = old.get("coverage", [])
    new_coverage = new.get("coverage", [])
    if len(old_coverage) != len(new_coverage):
        changed.add("coverage.rowCount")
    else:
        for index, (before, after) in enumerate(zip(old_coverage, new_coverage)):
            for key, field in (("country", "coverage.country"),
                               ("media_outlet_category", "coverage.mediaOutletCategory")):
                if before.get(key) != after.get(key):
                    changed.add(locator(field, index))
            for key in set(before) | set(after):
                if key not in {"country", "media_outlet_category"} and before.get(key) != after.get(key):
                    changed.add(locator(f"coverage.{key}", index))
    for key in ("media", "userGroups"):
        if old.get(key) != new.get(key):
            changed.add(key)
    for key in set(old["article"]) | set(new["article"]):
        if key not in {"category", "tone_sentiment"} and old["article"].get(key) != new["article"].get(key):
            changed.add(f"article.{key}")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = verify_bundle_dir(args.bundle_dir.resolve())
    ledger = json.loads(args.decisions.read_text(encoding="utf-8"))
    repairs: dict[str, set[str]] = {}
    for decision in ledger["decisions"]:
        if decision["disposition"] != "repair":
            continue
        repairs.setdefault(str(decision["sourceId"]), set()).add(
            locator(decision["field"], decision.get("index"))
        )
    records = load_projection_records(args.bundle_dir.resolve())
    new_by_id = {int(row["projection"]["article"]["article_id"]): row for row in records}
    old_values = [json.loads(line) for line in
                  (args.bundle_dir / "rollback-projections.ndjson").read_text(encoding="utf-8").splitlines()
                  if line]
    old_by_id = {int(value["article"]["article_id"]): value for value in old_values}
    update_ids = [int(value) for value in manifest["delta"]["updateArticleIds"]]
    failures = []
    field_counts = Counter()
    audited = []
    for article_id in update_ids:
        record = new_by_id[article_id]
        source_id = str(record["sourceId"])
        observed = changed_fields(old_by_id[article_id], record["projection"])
        allowed = repairs.get(source_id, set())
        if not observed or observed != allowed:
            failures.append({"sourceId": source_id, "articleId": article_id,
                             "observed": sorted(observed), "acceptedRepairs": sorted(allowed)})
        field_counts.update(observed)
        audited.append({"sourceId": source_id, "articleId": article_id,
                        "changedFields": sorted(observed)})
    report = {
        "status": "PASS" if not failures else "FAIL",
        "bundleId": manifest["bundleId"],
        "updates": len(update_ids),
        "deletes": manifest["delta"]["deletes"],
        "fieldCounts": dict(sorted(field_counts.items())),
        "failures": failures,
        "auditedUpdates": audited,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "bundleId", "updates", "deletes", "fieldCounts", "failures")}, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
