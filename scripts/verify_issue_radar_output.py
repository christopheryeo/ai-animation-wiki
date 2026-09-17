#!/usr/bin/env python3
"""Independently verify scores, tiers, ordering, and flag coverage in radar JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def expected_tier(score: float, volume: int, tiers: list[dict]) -> str | None:
    for tier in tiers:
        if score >= tier["minimumScore"] and volume >= tier["minimumRecentVolume"]:
            return tier["name"]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("radar_json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    radar = json.loads(args.radar_json.read_text(encoding="utf-8"))
    weights = radar["configuration"]["weights"]
    tiers = radar["configuration"]["tiers"]
    failures = []
    recomputed = []
    for flag in radar["flags"]:
        score = sum(weights[name] * flag["signals"][name] for name in weights)
        tier = expected_tier(score, flag["recentVolume"], tiers)
        row = {
            "tag": flag["tag"],
            "reportedScore": flag["score"],
            "recomputedScore": score,
            "reportedTier": flag["tier"],
            "recomputedTier": tier,
        }
        recomputed.append(row)
        if abs(score - flag["score"]) > 1e-12:
            failures.append({**row, "failure": "score mismatch"})
        if tier != flag["tier"]:
            failures.append({**row, "failure": "tier mismatch"})
        if not flag["reasons"]:
            failures.append({**row, "failure": "missing reasons"})
        if sorted(set(flag["articleIds"])) != flag["articleIds"]:
            failures.append({**row, "failure": "article IDs not unique and sorted"})
        if sorted(set(flag["recentArticleIds"])) != flag["recentArticleIds"]:
            failures.append({**row, "failure": "recent article IDs not unique and sorted"})
    expected_order = sorted(
        radar["flags"],
        key=lambda flag: (-flag["score"], flag["tag"]),
    )
    if [flag["tag"] for flag in expected_order] != [
        flag["tag"] for flag in radar["flags"]
    ]:
        failures.append({"failure": "flag ordering is not stable"})
    report = {
        "schemaVersion": "issue-radar-output-verification.v1",
        "source": str(args.radar_json),
        "asOf": radar["asOf"],
        "flagCount": len(radar["flags"]),
        "warmHotRecomputedCount": sum(
            flag["tier"] in {"WARM", "HOT"} for flag in radar["flags"]
        ),
        "watchRecomputedCount": sum(
            flag["tier"] == "WATCH" for flag in radar["flags"]
        ),
        "failureCount": len(failures),
        "failures": failures,
        "result": "PASS" if not failures else "FAIL",
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
