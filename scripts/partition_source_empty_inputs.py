#!/usr/bin/env python3
"""Partition a frozen loose-article batch into eligible inputs and exact source-empty holds.

The frozen manifest is authoritative. Every listed loose input must still exist at
the recorded path and retain its SHA-256 hash. An exact source-empty hold has a
non-empty article ID but empty title, publication date, URL, body, and an empty
``rawNewsApiResponse`` object. With ``--write``, only those exact holds are moved
unchanged into the supplied run-specific hold directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from enrich_radar_inputs import parse_frontmatter, split_note


ROOT = Path(__file__).resolve().parents[1]


class PartitionError(RuntimeError):
    pass


def is_empty_raw_response(value: Any) -> bool:
    if value == {}:
        return True
    if isinstance(value, str):
        try:
            return json.loads(value.strip() or "null") == {}
        except json.JSONDecodeError:
            return False
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-manifest", type=Path, required=True)
    parser.add_argument("--hold-dir", type=Path, required=True)
    parser.add_argument("--hold-manifest", type=Path, required=True)
    parser.add_argument("--eligible-manifest", type=Path, required=True)
    parser.add_argument("--eligible-evidence", type=Path, required=True)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    master = json.loads(args.master_manifest.resolve().read_text(encoding="utf-8"))
    articles = master.get("articles")
    if not isinstance(articles, list) or master.get("articleCount") != len(articles):
        raise PartitionError("invalid master manifest article count")

    holds: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    eligible_ids: set[str] = set()
    for row in articles:
        source = ROOT / str(row.get("path") or "")
        if not source.is_file():
            raise PartitionError(f"manifest input missing: {source}")
        raw = source.read_bytes()
        actual_hash = hashlib.sha256(raw).hexdigest()
        if actual_hash != row.get("sha256"):
            raise PartitionError(f"manifest hash drift: {source}")
        lines, body = split_note(raw.decode("utf-8"))
        metadata = parse_frontmatter(lines)
        article_id = str(metadata.get("articleId") or "").strip()
        if not article_id:
            raise PartitionError(f"missing article ID: {source}")
        exact_empty = (
            not str(metadata.get("articleTitle") or "").strip()
            and not str(metadata.get("publishedDate") or "").strip()
            and not str(metadata.get("url") or "").strip()
            and not body.strip()
            and is_empty_raw_response(metadata.get("rawNewsApiResponse"))
        )
        evidence = dict(row)
        evidence["sourcePath"] = str(source.relative_to(ROOT))
        if exact_empty:
            evidence["holdReason"] = "exact source-empty crawler stub"
            evidence["holdPath"] = str((args.hold_dir.resolve() / source.name).relative_to(ROOT))
            holds.append(evidence)
        else:
            if article_id in eligible_ids:
                raise PartitionError(f"duplicate eligible article ID: {source}")
            eligible_ids.add(article_id)
            eligible.append(evidence)

    if args.write:
        args.hold_dir.resolve().mkdir(parents=True, exist_ok=True)
        for row in holds:
            source = ROOT / row["sourcePath"]
            target = ROOT / row["holdPath"]
            if target.exists():
                raise PartitionError(f"hold target already exists: {target}")
            source.replace(target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != row["sha256"]:
                raise PartitionError(f"hash changed while moving hold: {target}")

    hold_payload = {
        "schemaVersion": "source-empty-holds.v1",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "write" if args.write else "dry-run",
        "holdCount": len(holds),
        "holds": holds,
    }
    eligible_payload = {
        "schemaVersion": "loose-article-manifest.v1",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "articleCount": len(eligible),
        "articles": eligible,
    }
    for path in [args.hold_manifest, args.eligible_manifest, args.eligible_evidence]:
        path.resolve().parent.mkdir(parents=True, exist_ok=True)
    args.hold_manifest.resolve().write_text(
        json.dumps(hold_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.eligible_manifest.resolve().write_text(
        "\n".join(row["sourcePath"] for row in eligible) + ("\n" if eligible else ""),
        encoding="utf-8",
    )
    args.eligible_evidence.resolve().write_text(
        json.dumps(eligible_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "mode": hold_payload["mode"],
        "masterCount": len(articles),
        "eligibleCount": len(eligible),
        "holdCount": len(holds),
        "holdDirectory": str(args.hold_dir),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
