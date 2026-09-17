#!/usr/bin/env python3
"""Verify Tag shadow optimization against baseline, benchmark, and precision gates."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import yaml


RAMIFICATION = {"low": 0, "moderate": 1, "high": 2, "severe": 3}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def flag_map(payload):
    return {
        row["tag"]: {key: row[key] for key in ("tier", "score", "recentVolume", "signals", "reasons")}
        for row in payload["flags"]
    }


def issue_metadata(root: Path, issue_id: str):
    text = (root / f"{issue_id}.md").read_text(encoding="utf-8")
    match = re.match(r"(?s)^---\n(.*?)\n---", text)
    return yaml.safe_load(match.group(1))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled", type=Path, required=True)
    parser.add_argument("--enabled-repeat", type=Path, required=True)
    parser.add_argument("--shadow", type=Path, required=True)
    parser.add_argument("--shadow-repeat", type=Path, required=True)
    parser.add_argument("--comparison", type=Path, required=True)
    parser.add_argument("--comparison-repeat", type=Path, required=True)
    parser.add_argument("--benchmark-enabled", type=Path, required=True)
    parser.add_argument("--benchmark-enabled-repeat", type=Path, required=True)
    parser.add_argument("--benchmark-shadow", type=Path, required=True)
    parser.add_argument("--benchmark-comparison", type=Path, required=True)
    parser.add_argument("--benchmarks", type=Path, required=True)
    parser.add_argument("--issues-root", type=Path, required=True)
    parser.add_argument("--prior-accuracy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    enabled = load(args.enabled)
    shadow = load(args.shadow)
    comparison = load(args.comparison)
    benchmark_enabled = load(args.benchmark_enabled)
    benchmark_shadow = load(args.benchmark_shadow)
    benchmark_comparison = load(args.benchmark_comparison)
    accuracy = load(args.prior_accuracy)
    failures = []

    repeat_pairs = [
        ("enabled", args.enabled, args.enabled_repeat),
        ("shadow", args.shadow, args.shadow_repeat),
        ("comparison", args.comparison, args.comparison_repeat),
        ("benchmark-enabled", args.benchmark_enabled, args.benchmark_enabled_repeat),
    ]
    repeatability = {}
    for label, first, second in repeat_pairs:
        same = first.read_bytes() == second.read_bytes()
        repeatability[label] = {
            "identical": same,
            "sha256": file_hash(first),
        }
        if not same:
            failures.append(f"{label} repeated output differs")

    latest_lost = sorted(set(flag_map(comparison)) - set(flag_map(enabled)))
    latest_added = sorted(set(flag_map(enabled)) - set(flag_map(comparison)))
    benchmark_lost = sorted(set(flag_map(benchmark_comparison)) - set(flag_map(benchmark_enabled)))
    if latest_lost or latest_added:
        failures.append("latest enabled flags differ from enabled-plus-shadow comparison")
    if benchmark_lost:
        failures.append("historical benchmark flags were lost")

    benchmark_flags = flag_map(benchmark_enabled)
    benchmark_rows = []
    severe_high_misses = 0
    for expected in load(args.benchmarks)["benchmarks"]:
        metadata = issue_metadata(args.issues_root, expected["issueId"])
        hits = sorted(
            tag for tag in metadata.get("clusterTags", []) if tag.casefold() in benchmark_flags
        )
        passed = bool(hits) and (
            RAMIFICATION[metadata["ramification"]]
            >= RAMIFICATION[expected["expectedMinimumRamification"]]
        )
        if expected["expectedDisposition"] == "dismissed":
            passed = passed and metadata["status"] == "dismissed"
        if not passed and expected["expectedMinimumRamification"] in {"high", "severe"}:
            severe_high_misses += 1
        benchmark_rows.append({
            "issueId": expected["issueId"], "pass": passed,
            "matchingTags": hits, "ramification": metadata["ramification"],
        })
        if not passed:
            failures.append(f"benchmark failed: {expected['issueId']}")

    precision = float(accuracy.get("surfacedAlertPrecision", 0))
    if precision < 0.8:
        failures.append("maintained surfaced-alert precision is below 80%")
    status_counts = enabled["tagRegistry"]["radarStatusCounts"]
    total = enabled["tagRegistry"]["tagCount"]
    shadow_count = status_counts.get("shadow", 0)
    reduction = shadow_count / total if total else 0
    minimum_count = int(total * 0.20)
    maximum_count = int(total * 0.30)
    if not minimum_count <= shadow_count <= maximum_count:
        failures.append("shadow working-set reduction is outside 20-30%")

    payload = {
        "schemaVersion": "tag-radar-optimization-verification.v1",
        "result": "PASS" if not failures else "FAIL",
        "failureCount": len(failures),
        "failures": failures,
        "tagCount": total,
        "enabledCount": status_counts.get("enabled", 0),
        "shadowCount": shadow_count,
        "reductionFraction": reduction,
        "acceptedCountRange": {"minimum": minimum_count, "maximum": maximum_count},
        "latest": {
            "comparisonFlags": comparison["flagCount"],
            "enabledFlags": enabled["flagCount"],
            "shadowFlags": shadow["flagCount"],
            "lostFlags": latest_lost,
            "addedFlags": latest_added,
        },
        "historicalBenchmark": {
            "comparisonFlags": benchmark_comparison["flagCount"],
            "enabledFlags": benchmark_enabled["flagCount"],
            "shadowFlags": benchmark_shadow["flagCount"],
            "lostFlags": benchmark_lost,
            "knownSevereHighMisses": severe_high_misses,
            "cases": benchmark_rows,
        },
        "maintainedPrecision": precision,
        "requiredPrecision": 0.8,
        "repeatability": repeatability,
        "databaseWrites": 0,
        "productionWrites": 0,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
