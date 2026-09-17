#!/usr/bin/env python3
"""Apply a conservative, fully local two-pass review to a radar evidence pack.

This is the no-data-egress review path. It never calls a network service. The
two deterministic passes can retain or dismiss a candidate, but they cannot
surface an alert by themselves. Surfacing requires a separately recorded,
evidence-cited decision file produced by the judgment procedure.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


DOMESTIC = re.compile(
    r"\b(united states|southeast asia|animation studio|character owner|licensee|"
    r"merchandis|streaming platform|production company)\b",
    re.IGNORECASE,
)
FORCED_RESPONSE = re.compile(
    r"\b(studio|rights holder|licensor|licensee|distributor|platform|regulator|agency)\b",
    re.IGNORECASE,
)
FAULT_LINE = re.compile(
    r"\b(copyright|ownership|licensing dispute|royalty|labor|workforce|"
    r"artificial intelligence|deepfake|cultural sensitivity|market access|"
    r"distribution dispute|consumer safety)\b",
    re.IGNORECASE,
)


def cluster_text(cluster: dict) -> str:
    parts = [cluster["seedTag"]]
    parts.extend(member["flag"]["tag"] for member in cluster["members"])
    for article in cluster["evidenceArticles"][:12]:
        parts.extend((article["title"], article["summary"], article["category"]))
    return "\n".join(parts)


def evidence_metrics(cluster: dict) -> dict:
    articles = cluster["evidenceArticles"]
    count = max(1, len(articles))
    titles = [
        re.sub(r"\W+", " ", article["title"].casefold()).strip()
        for article in articles
        if article["title"].strip()
    ]
    largest_title_group = max((titles.count(title) for title in set(titles)), default=0)
    text = cluster_text(cluster)
    return {
        "articleCount": len(articles),
        "domesticEvidence": bool(DOMESTIC.search(text)),
        "forcedResponderEvidence": bool(FORCED_RESPONSE.search(text)),
        "faultLineEvidence": bool(FAULT_LINE.search(text)),
        "facilitatedShare": (
            sum(article["eventType"] == "Facilitated" for article in articles) / count
        ),
        "unfacilitatedShare": (
            sum(article["eventType"] == "Unfacilitated" for article in articles) / count
        ),
        "opinionatedShare": (
            sum(article["tone"] == "Opinionated" for article in articles) / count
        ),
        "largestExactTitleShare": largest_title_group / count,
    }


def primary_review(metrics: dict) -> dict:
    if not metrics["domesticEvidence"]:
        return {
            "disposition": "dismiss",
            "reason": "No Southeast Asia-linked evidence in the compiled article sample.",
        }
    if (
        metrics["facilitatedShare"] >= 0.60
        and metrics["largestExactTitleShare"] >= 0.45
    ):
        return {
            "disposition": "dismiss",
            "reason": "Predominantly facilitated, event-shaped repeat coverage.",
        }
    if metrics["faultLineEvidence"] and metrics["unfacilitatedShare"] >= 0.35:
        return {
            "disposition": "quiet-watch",
            "reason": "Domestic fault-line language merits evidence review but does not prove ramification.",
        }
    return {
        "disposition": "dismiss",
        "reason": "Domestic mention is present, but the evidence does not establish a fault line.",
    }


def second_review(metrics: dict, primary: dict) -> dict:
    if (
        primary["disposition"] == "quiet-watch"
        and metrics["forcedResponderEvidence"]
        and metrics["faultLineEvidence"]
        and metrics["unfacilitatedShare"] >= 0.35
    ):
        return {
            "disposition": "quiet-watch",
            "reason": "Forced-responder and fault-line evidence both survive the stricter pass.",
        }
    return {
        "disposition": "dismiss",
        "reason": "The stricter forced-response/fault-line gate was not met.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-pack", required=True, type=Path)
    parser.add_argument("--decisions", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    pack = json.loads(args.review_pack.read_text(encoding="utf-8"))
    decisions = json.loads(args.decisions.read_text(encoding="utf-8"))
    overrides = {
        decision["clusterId"]: decision for decision in decisions["decisions"]
    }
    known_ids = {cluster["clusterId"] for cluster in pack["clusters"]}
    unknown = sorted(set(overrides) - known_ids)
    if unknown:
        raise SystemExit(f"decision file contains unknown clusters: {unknown}")

    assessments = []
    for cluster in pack["clusters"]:
        metrics = evidence_metrics(cluster)
        if cluster["highestTier"] == "WATCH":
            primary = {
                "disposition": "quiet-watch",
                "reason": "WATCH retained below the alert threshold.",
            }
            second = primary
        else:
            primary = primary_review(metrics)
            second = second_review(metrics, primary)
        final = dict(second)
        final["source"] = "local-two-pass-rubric"
        if cluster["clusterId"] in overrides:
            final = dict(overrides[cluster["clusterId"]])
            final["source"] = "recorded-evidence-judgment"
        assessments.append(
            {
                "clusterId": cluster["clusterId"],
                "seedTag": cluster["seedTag"],
                "highestTier": cluster["highestTier"],
                "highestScore": cluster["highestScore"],
                "memberTags": [member["flag"]["tag"] for member in cluster["members"]],
                "metrics": metrics,
                "primaryReview": primary,
                "secondReview": second,
                "final": final,
            }
        )

    output = {
        "schemaVersion": "issue-radar-local-review.v1",
        "sourceReviewPack": str(args.review_pack),
        "decisionFile": str(args.decisions),
        "clusterCount": len(assessments),
        "flagCount": pack["flagCount"],
        "warmHotReviewedCount": sum(
            item["highestTier"] in {"WARM", "HOT"} for item in assessments
        ),
        "surfaceClusterCount": sum(
            item["final"]["disposition"].startswith("surface-")
            for item in assessments
        ),
        "assessments": assessments,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"Reviewed {output['clusterCount']} clusters; "
        f"{output['warmHotReviewedCount']} WARM/HOT; "
        f"{output['surfaceClusterCount']} surface clusters."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
