#!/usr/bin/env python3
"""Build an evidence pack and complete tag disposition ledger for a radar run.

This helper deliberately does not decide whether an issue is important. It groups
tag flags only when their recent supporting-article sets overlap strongly, records
the exact overlap basis, and resolves article IDs to compiled Markdown evidence.
The judgment pass in issue_radar_procedure.md remains responsible for ramification,
catalysts, posture, and filing.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


PROJECTION_RE = re.compile(
    r"## Database Projection\s*```json\s*(\{.*?\})\s*```", re.DOTALL
)
SUMMARY_RE = re.compile(r"## Summary\s*(.*?)(?=\n## |\Z)", re.DOTALL)
TIER_ORDER = {"HOT": 3, "WARM": 2, "WATCH": 1}


def article_index(article_root: Path) -> dict[int, dict]:
    """Index only the source-backed fields needed by the review pack."""
    indexed: dict[int, dict] = {}
    for path in sorted(article_root.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = PROJECTION_RE.search(text)
        if not match:
            continue
        try:
            projection = json.loads(match.group(1))
            article = projection["article"]
            article_id = int(article["article_id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        indexed[article_id] = {
            "articleId": article_id,
            "title": article.get("article_title") or article.get("content_title") or "",
            "publishedDate": str(article.get("published_date") or "")[:10],
            "category": article.get("category") or "",
            "tone": article.get("tone") or "",
            "eventType": article.get("event_type") or "",
            "tags": projection.get("tags") or [],
            "outlets": [
                item.get("display_name") or ""
                for item in (projection.get("coverage") or [])
                if item.get("display_name")
            ],
            "countries": [
                item.get("country") or ""
                for item in (projection.get("coverage") or [])
                if item.get("country")
            ],
            "summary": (
                " ".join(SUMMARY_RE.search(text).group(1).split())[:1800]
                if SUMMARY_RE.search(text)
                else ""
            ),
            "path": str(path),
        }
    return indexed


def flag_key(flag: dict) -> tuple:
    return (
        -TIER_ORDER.get(flag["tier"], 0),
        -float(flag["score"]),
        -int(flag["recentVolume"]),
        flag["tag"],
    )


def overlap(left: set[int], right: set[int]) -> dict:
    shared = left & right
    return {
        "sharedArticleCount": len(shared),
        "candidateCoverage": len(shared) / len(right) if right else 0.0,
        "seedCoverage": len(shared) / len(left) if left else 0.0,
        "jaccard": len(shared) / len(left | right) if left or right else 0.0,
        "sharedArticleIds": sorted(shared),
    }


def qualifies(metrics: dict) -> bool:
    """Use a non-transitive, two-sided test so broad bridge tags cannot snowball."""
    return (
        metrics["sharedArticleCount"] >= 2
        and (
            (
                metrics["candidateCoverage"] >= 0.65
                and metrics["seedCoverage"] >= 0.25
            )
            or metrics["jaccard"] >= 0.35
        )
    )


def cluster_flags(flags: list[dict]) -> list[dict]:
    """Create star clusters around ranked seeds; never merge clusters transitively."""
    remaining = {flag["tag"]: flag for flag in flags}
    clusters: list[dict] = []
    for seed in sorted(flags, key=flag_key):
        if seed["tag"] not in remaining:
            continue
        remaining.pop(seed["tag"])
        seed_ids = set(seed["recentArticleIds"])
        members = [{"flag": seed, "overlapWithSeed": None}]
        ranked_matches = []
        for candidate in remaining.values():
            metrics = overlap(seed_ids, set(candidate["recentArticleIds"]))
            if qualifies(metrics):
                ranked_matches.append((candidate, metrics))
        ranked_matches.sort(
            key=lambda item: (
                -item[1]["jaccard"],
                -item[1]["sharedArticleCount"],
                flag_key(item[0]),
            )
        )
        for candidate, metrics in ranked_matches:
            if candidate["tag"] not in remaining:
                continue
            remaining.pop(candidate["tag"])
            members.append({"flag": candidate, "overlapWithSeed": metrics})
        clusters.append(
            {
                "clusterId": f"cluster-{len(clusters) + 1:04d}",
                "seedTag": seed["tag"],
                "highestTier": max(
                    (member["flag"]["tier"] for member in members),
                    key=lambda tier: TIER_ORDER[tier],
                ),
                "highestScore": max(member["flag"]["score"] for member in members),
                "members": members,
            }
        )
    return clusters


def make_outputs(radar: dict, indexed: dict[int, dict]) -> tuple[dict, str]:
    clusters = cluster_flags(radar["flags"])
    flag_rows = []
    for cluster in clusters:
        article_ids = sorted(
            {
                article_id
                for member in cluster["members"]
                for article_id in member["flag"]["recentArticleIds"]
            }
        )
        cluster["recentArticleIds"] = article_ids
        article_support = {}
        for article_id in article_ids:
            supporting_tags = sorted(
                member["flag"]["tag"]
                for member in cluster["members"]
                if article_id in member["flag"]["recentArticleIds"]
            )
            article_support[article_id] = supporting_tags
        cluster["evidenceArticles"] = sorted(
            (
                {
                    **indexed[article_id],
                    "supportingTags": article_support[article_id],
                    "supportingTagCount": len(article_support[article_id]),
                }
                for article_id in article_ids
                if article_id in indexed
            ),
            key=lambda article: (
                -article["supportingTagCount"],
                article["publishedDate"],
                article["articleId"],
            ),
        )
        cluster["missingArticleIds"] = [
            article_id for article_id in article_ids if article_id not in indexed
        ]
        cluster["reviewDisposition"] = (
            "evidence-review-required"
            if cluster["highestTier"] in {"WARM", "HOT"}
            else "quiet-watch"
        )
        cluster["reviewReason"] = (
            "A WARM/HOT cluster requires the procedure's evidence-only ramification review."
            if cluster["highestTier"] in {"WARM", "HOT"}
            else "WATCH retained below the alert threshold for rotating review."
        )
        for member in cluster["members"]:
            flag = member["flag"]
            flag_rows.append(
                {
                    "tag": flag["tag"],
                    "tier": flag["tier"],
                    "score": flag["score"],
                    "clusterId": cluster["clusterId"],
                    "clusterSeed": cluster["seedTag"],
                    "disposition": cluster["reviewDisposition"],
                    "basis": (
                        "ranked cluster seed"
                        if member["overlapWithSeed"] is None
                        else member["overlapWithSeed"]
                    ),
                }
            )

    structured = {
        "schemaVersion": "issue-radar-review-pack.v1",
        "radarSource": radar["source"],
        "asOf": radar["asOf"],
        "flagCount": len(radar["flags"]),
        "clusterCount": len(clusters),
        "resolvedArticleCount": len(indexed),
        "clusteringRule": {
            "method": "ranked non-transitive star clusters",
            "minimumSharedArticles": 2,
            "qualification": (
                "(candidate coverage >= 0.65 and seed coverage >= 0.25) "
                "or Jaccard >= 0.35"
            ),
            "nameSimilarityUsed": False,
        },
        "flagDispositions": sorted(flag_rows, key=lambda row: row["tag"]),
        "clusters": clusters,
    }

    counts: dict[str, int] = {}
    for cluster in clusters:
        counts[cluster["highestTier"]] = counts.get(cluster["highestTier"], 0) + 1
    lines = [
        "# Issue Radar Evidence Review Pack",
        "",
        f"- Source: `{radar['source']}`",
        f"- Evaluation date: `{radar['asOf']}`",
        f"- Tag flags: **{len(radar['flags'])}**",
        f"- Evidence clusters: **{len(clusters)}**",
        f"- Cluster tiers: HOT {counts.get('HOT', 0)}, WARM {counts.get('WARM', 0)}, "
        f"WATCH {counts.get('WATCH', 0)}",
        "- Clustering: shared recent articles only; tag-name similarity was not used.",
        "",
        "## WARM/HOT evidence-review queue",
        "",
    ]
    for cluster in clusters:
        if cluster["highestTier"] not in {"WARM", "HOT"}:
            continue
        tags = ", ".join(member["flag"]["tag"] for member in cluster["members"])
        lines.extend(
            [
                f"### {cluster['clusterId']} — {cluster['seedTag']}",
                "",
                f"- Tier/score: {cluster['highestTier']} / {cluster['highestScore']:.3f}",
                f"- Tags: {tags}",
                f"- Recent supporting articles: {len(cluster['recentArticleIds'])}",
            ]
        )
        for article in cluster["evidenceArticles"][:8]:
            lines.append(
                f"- Evidence: `{article['articleId']}` — {article['title']} "
                f"({article['publishedDate']}; {article['category']}; "
                f"{article['eventType']}; {article['tone']})"
            )
        if cluster["missingArticleIds"]:
            lines.append(
                "- Unresolved article IDs: "
                + ", ".join(map(str, cluster["missingArticleIds"]))
            )
        lines.append("")
    return structured, "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--radar-json", required=True, type=Path)
    parser.add_argument("--article-root", default="entities/article", type=Path)
    parser.add_argument("--json-output", required=True, type=Path)
    parser.add_argument("--markdown-output", required=True, type=Path)
    parser.add_argument("--manifest-output", type=Path)
    args = parser.parse_args()

    radar = json.loads(args.radar_json.read_text(encoding="utf-8"))
    indexed = article_index(args.article_root)
    structured, markdown = make_outputs(radar, indexed)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(structured, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    args.markdown_output.write_text(markdown, encoding="utf-8")
    if args.manifest_output:
        eligible = sorted(
            (
                article for article in indexed.values()
                if article["publishedDate"] and article["publishedDate"] <= radar["asOf"]
            ),
            key=lambda article: article["articleId"],
        )
        manifest = {
            "schemaVersion": "issue-radar-article-manifest.v1",
            "source": radar["source"],
            "asOf": radar["asOf"],
            "articleCount": len(eligible),
            "articleIds": [article["articleId"] for article in eligible],
            "totals": {
                "tagOccurrences": sum(len(article["tags"]) for article in eligible),
                "uniqueTags": len({
                    tag.casefold() for article in eligible for tag in article["tags"]
                }),
                "outletOccurrences": sum(len(article["outlets"]) for article in eligible),
                "uniqueOutlets": len({
                    outlet.casefold()
                    for article in eligible for outlet in article["outlets"]
                }),
                "countryOccurrences": sum(len(article["countries"]) for article in eligible),
                "uniqueCountries": len({
                    country.casefold()
                    for article in eligible for country in article["countries"]
                }),
                "categories": sorted({
                    article["category"] for article in eligible if article["category"]
                }),
                "tones": sorted({
                    article["tone"] for article in eligible if article["tone"]
                }),
                "eventTypes": sorted({
                    article["eventType"] for article in eligible if article["eventType"]
                }),
            },
        }
        args.manifest_output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(
        f"Wrote {structured['flagCount']} flag dispositions across "
        f"{structured['clusterCount']} evidence clusters."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
