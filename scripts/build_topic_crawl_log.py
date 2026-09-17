#!/usr/bin/env python3
"""Generate a `## Crawl Log` section in each Topic Entity note.

The section is a *projection* of the append-only crawl history recorded in
``entities/topic/log.md`` (the source of truth) plus each note's current crawl
frontmatter. It is regenerated wholesale and is safe to re-run (idempotent):
only the `## Crawl Log` section is inserted or replaced; all other content is
preserved byte-for-byte.

Coverage of the backfill is uneven by design — it reflects what was actually
recorded. Granular per-episode logging (`action: crawl queued/started/
completed/failed`) began in 2026-09, so older topics show only their latest
frontmatter status plus their article-addition history.

Usage:
  python3 scripts/build_topic_crawl_log.py            # dry run: print a summary
  python3 scripts/build_topic_crawl_log.py --only <topicId> [<topicId> ...] --show
  python3 scripts/build_topic_crawl_log.py --write    # apply to every topic note
"""
from __future__ import annotations
import argparse
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOPIC_DIR = ROOT / "entities" / "topic"
LOG = TOPIC_DIR / "log.md"
SYSTEM = {"index.md", "catalog.md", "log.md", "_template.md"}
SECTION = "## Crawl Log"
MARKER = "<!-- Generated from entities/topic/log.md by scripts/build_topic_crawl_log.py — do not hand-edit. -->"

ACTION_RE = re.compile(
    r"^-?\s*(?P<ts>\S+)\s*\|\s*entity:\s*\[\[(?P<tid>[a-z0-9-]+)\|[^\]]*\]\]\s*\|\s*"
    r"action:\s*(?P<action>crawl[a-z ]*?)\s*\|(?P<rest>.*)$"
)
COVER_RE = re.compile(
    r"^-?\s*(?P<date>\d{4}-\d{2}-\d{2})\b.*Added coverage\s*\[\[article/[^\]]*\]\]\s*to\s*\[\[(?P<tid>[a-z0-9-]+)\|"
)


def parse_log():
    """Return (episodes_by_topic, coverage_months_by_topic)."""
    raw_actions = defaultdict(list)
    coverage = defaultdict(lambda: defaultdict(int))  # tid -> {YYYY-MM: count}
    if not LOG.exists():
        return {}, {}
    for line in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ACTION_RE.match(line)
        if m:
            rest = m.group("rest")
            reason = ""
            rm = re.search(r"reason:\s*(.*?)(?:\s*\|\s*source:.*)?$", rest)
            if rm:
                reason = rm.group(1).strip()
            cp = re.search(r"checkpoint:\s*(\S+)", rest)
            raw_actions[m.group("tid")].append({
                "ts": m.group("ts"),
                "action": m.group("action").strip(),
                "reason": reason,
                "checkpoint": cp.group(1) if cp else "",
            })
            continue
        c = COVER_RE.match(line)
        if c:
            coverage[c.group("tid")][c.group("date")[:7]] += 1

    # Group raw actions into episodes (start -> terminal).
    def num(pat, text):
        mm = re.search(pat, text)
        return mm.group(1) if mm else ""
    episodes = defaultdict(list)
    for tid, acts in raw_actions.items():
        acts.sort(key=lambda a: a["ts"])
        cur = None
        for a in acts:
            act = a["action"]
            if act in ("crawl queued", "crawl started", "crawl status transition"):
                if cur is None:
                    dr = re.search(r"for\s*(\d{4}-\d{2}-\d{2})\s*through\s*(\d{4}-\d{2}-\d{2})", a["reason"])
                    cur = {"started": a["ts"], "range": f"{dr.group(1)}→{dr.group(2)}" if dr else "",
                           "result": "incomplete", "accepted": "", "held": "", "note": a["reason"]}
            if act in ("crawl completed", "crawl completion checkpoint",
                       "crawl failed", "crawl failure recorded"):
                if cur is None:
                    cur = {"started": a["ts"], "range": "", "result": "", "accepted": "", "held": "", "note": ""}
                cur["result"] = "Completed" if "complet" in act else "Failed"
                cur["ended"] = a["ts"]
                cur["accepted"] = num(r"(\d+)\s+accepted", a["reason"])
                cur["held"] = num(r"(\d+)\s+held", a["reason"])
                if not cur["accepted"] and re.search(r"zero results|no .*results", a["reason"], re.I):
                    cur["accepted"] = "0"
                cur["note"] = a["reason"]
                episodes[tid].append(cur)
                cur = None
        if cur is not None:  # open episode with no terminal
            episodes[tid].append(cur)
    return episodes, coverage


def read_frontmatter(text):
    m = re.search(r"^---\n(.*?)\n---", text, re.S)
    fm = m.group(1) if m else ""
    def g(k):
        mm = re.search(rf"^{k}:\s*(.+)$", fm, re.M)
        value = mm.group(1).strip() if mm else ""
        return "" if value.lower() in {"null", "none"} else value
    return g


def build_section(tid, episodes, coverage, g):
    lines = [SECTION, MARKER, ""]
    status = g("crawlStatus") or "—"
    lines.append(f"**Latest status:** `{status}` (at {g('crawlStatusAt') or '—'}); "
                 f"last successful crawl: {g('lastCrawledAt') or 'never'}.")
    lines.append("")
    eps = episodes.get(tid, [])
    if eps:
        lines.append("**Logged crawl episodes** (most recent first):")
        lines.append("")
        lines.append("| Started | Date range | Result | Accepted | Held/Rej | Note |")
        lines.append("|---|---|---|---|---|---|")
        for e in sorted(eps, key=lambda x: x["started"], reverse=True):
            res = {"Completed": "✅ Completed", "Failed": "❌ Failed"}.get(e["result"], e["result"] or "—")
            note = (e.get("note") or "").replace("|", "／")
            note = (note[:90] + "…") if len(note) > 90 else note
            lines.append(f"| {e['started'][:16]} | {e.get('range') or '—'} | {res} | "
                         f"{e.get('accepted') or '—'} | {e.get('held') or '—'} | {note} |")
    else:
        lines.append("_No granular crawl episodes were logged for this topic "
                     "(episode-level logging began 2026-09)._")
    lines.append("")
    months = coverage.get(tid, {})
    if months:
        total = sum(months.values())
        by = ", ".join(f"{m}: {months[m]}" for m in sorted(months))
        lines.append(f"**Articles added to coverage:** {total} total — {by}.")
    else:
        lines.append("**Articles added to coverage:** none recorded.")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def inject(text, section):
    """Insert or replace the ## Crawl Log section, preserving everything else."""
    lines = text.splitlines(keepends=True)
    # find existing section bounds
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == SECTION:
            start = i
            break
    if start is not None:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        return "".join(lines[:start]) + section + ("\n" if not section.endswith("\n\n") else "") + "".join(lines[end:])
    # insert before '## Notes' if present, else append at end
    ins = len(lines)
    for i, ln in enumerate(lines):
        if ln.strip() == "## Notes":
            ins = i
            break
    body = "".join(lines[:ins])
    if not body.endswith("\n"):
        body += "\n"
    if not body.endswith("\n\n"):
        body += "\n"
    return body + section + "\n" + "".join(lines[ins:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="apply changes to topic notes")
    ap.add_argument("--only", nargs="*", default=None, help="restrict to these topicIds")
    ap.add_argument("--show", action="store_true", help="print the generated section(s)")
    args = ap.parse_args()

    episodes, coverage = parse_log()
    notes = [p for p in sorted(TOPIC_DIR.glob("*.md")) if p.name not in SYSTEM and "conflicted copy" not in p.name]
    changed = 0
    with_eps = 0
    for p in notes:
        tid = p.stem
        if args.only and tid not in args.only:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        g = read_frontmatter(text)
        section = build_section(tid, episodes, coverage, g)
        if episodes.get(tid):
            with_eps += 1
        if args.show:
            print(f"\n===== {tid} =====\n{section}")
        new = inject(text, section)
        if new != text:
            changed += 1
            if args.write:
                p.write_text(new, encoding="utf-8")
    print(f"\n{'WROTE' if args.write else 'DRY-RUN'}: {changed} topic notes "
          f"{'updated' if args.write else 'would change'} "
          f"(of {len([p for p in notes if not args.only or p.stem in args.only])} scanned); "
          f"{with_eps} have logged episodes.")


if __name__ == "__main__":
    main()
