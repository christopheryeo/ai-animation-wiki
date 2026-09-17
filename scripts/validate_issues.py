#!/usr/bin/env python3
"""Validate the frozen Issues registry and wikilinks in issue notes."""

from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
ISSUES = ROOT / "entities" / "issues"
REQUIRED = {
    "issueId", "displayName", "status", "ramification", "score",
    "firstFlagged", "lastScored", "clusterTags", "aliases", "articleCount",
}
STATUS = {"watch", "warm", "hot", "dismissed", "closed"}
RAMIFICATION = {"low", "moderate", "high", "severe"}
LINK_RE = re.compile(r"\[\[([^|\]]+)(?:\|[^\]]+)?\]\]")
SIGNAL_ROWS = {
    "Acceleration", "Breadth expansion", "Institutional attachment", "Recurrence",
    "Unfacilitated share", "Opinionated share",
}


def frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    fields = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def main() -> int:
    notes = sorted(
        path for path in ISSUES.glob("*.md")
        if path.name not in {"index.md", "catalog.md", "log.md"}
    )
    all_notes = list((ROOT / "entities").rglob("*.md"))
    by_stem = {}
    by_relative = set()
    for path in all_notes:
        by_stem.setdefault(path.stem, []).append(path)
        by_relative.add(str(path.relative_to(ROOT / "entities").with_suffix("")))
    failures = []
    issue_ids = {}
    for path in notes:
        text = path.read_text(encoding="utf-8")
        fields = frontmatter(text)
        missing = sorted(REQUIRED - set(fields))
        if missing:
            failures.append({"path": str(path), "failure": f"missing fields: {missing}"})
            continue
        issue_id = fields["issueId"]
        issue_ids.setdefault(issue_id, []).append(str(path))
        if path.stem != issue_id:
            failures.append({"path": str(path), "failure": "filename does not equal issueId"})
        if fields["status"] not in STATUS:
            failures.append({"path": str(path), "failure": "invalid status"})
        if fields["ramification"] not in RAMIFICATION:
            failures.append({"path": str(path), "failure": "invalid ramification"})
        try:
            score = float(fields["score"])
            article_count = int(fields["articleCount"])
        except ValueError:
            failures.append({"path": str(path), "failure": "invalid numeric field"})
            continue
        if not 0 <= score <= 1 or article_count < 0:
            failures.append({"path": str(path), "failure": "numeric field out of range"})
        if fields["status"] == "hot" and score < 0.60:
            failures.append({"path": str(path), "failure": "HOT score below 0.60"})
        if fields["status"] == "warm" and score < 0.40:
            failures.append({"path": str(path), "failure": "WARM score below 0.40"})
        if "## Signal Scores\n" not in text:
            failures.append({"path": str(path), "failure": "missing ## Signal Scores section"})
        else:
            missing_signal_rows = sorted(
                signal for signal in SIGNAL_ROWS if f"| {signal} |" not in text
            )
            if missing_signal_rows:
                failures.append({
                    "path": str(path),
                    "failure": f"missing signal-score rows: {missing_signal_rows}",
                })
        for target in LINK_RE.findall(text):
            target = target.strip()
            if target in by_relative:
                continue
            if target in by_stem:
                continue
            failures.append({
                "path": str(path),
                "failure": f"unresolved wikilink: {target}",
            })
    for issue_id, paths in issue_ids.items():
        if len(paths) > 1:
            failures.append({"issueId": issue_id, "failure": "duplicate issueId", "paths": paths})
    catalog = (ISSUES / "catalog.md").read_text(encoding="utf-8")
    for issue_id in issue_ids:
        if issue_id not in catalog:
            failures.append({"issueId": issue_id, "failure": "missing from generated catalog"})
    report = {
        "schemaVersion": "issues-validation.v2",
        "issueCount": len(notes),
        "failureCount": len(failures),
        "failures": failures,
        "result": "PASS" if not failures else "FAIL",
    }
    print(json.dumps(report, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
