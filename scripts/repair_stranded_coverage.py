#!/usr/bin/env python3
"""Move stranded article backlinks back into an entity note's ## Coverage block.

Why this exists
---------------
``ingest_cascade.py`` used to carry its own inline copy of the Coverage-append
logic which did ``text.rstrip() + "- [[article/...]]"`` — appending to the END
OF THE FILE rather than into the ``## Coverage`` section. On any entity note
whose ``## Coverage`` is not the last section (country notes are laid out
``## Coverage`` -> ``## Outlets Based Here`` -> ``## Notes``), every cascaded
backlink landed under the trailing section instead.

That inline copy has since been replaced by a delegation to
``patch_coverage.py``, which inserts correctly. This script repairs the notes
already damaged by the old behaviour.

What it moves
-------------
Only lines matching ``- [[article/...]]`` that sit OUTSIDE the ``## Coverage``
block. Prose, ``[[entity]]`` links, and outlet links in other sections are left
alone — the ``article/`` target prefix is the discriminator.

What it deliberately does NOT do
--------------------------------
It does not touch ``mentionCount``/``articleCount``. Those were already
incremented correctly when the link was first appended; only the line's
placement was wrong. Re-counting here would double-count.

Deduplication: if a stranded link's target is already present in the Coverage
block, the stranded copy is dropped rather than duplicated.

Usage:
  python3 scripts/repair_stranded_coverage.py            # dry run (default)
  python3 scripts/repair_stranded_coverage.py --write    # apply
  python3 scripts/repair_stranded_coverage.py --write --log   # apply + append domain logs
  python3 scripts/repair_stranded_coverage.py --report out.txt
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTITIES = ROOT / "entities"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}

# Domains whose notes carry a ## Coverage block built by the cascade.
DOMAINS = ["country", "people", "organisations", "place", "outlet", "topic"]

# Greedy to the LAST "]]" on the line: article display labels legitimately
# contain "]" (e.g. "[Southeast Asia Airshow] Australian F-35A..."), which a
# character-class match would stop short on and silently skip.
ARTICLE_LINE = re.compile(r"^\s*-\s*\[\[article/.*\]\]\s*$")
LINK_TARGET = re.compile(r"\[\[([^|\]]+)")


def split_sections(text: str) -> tuple[str, list[tuple[str, list[str]]]]:
    """Split note body into (preamble, [(header_line, [body_lines]), ...])."""
    lines = text.split("\n")
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in lines:
        if line.startswith("## "):
            if current:
                sections.append(current)
            current = (line, [])
        elif current:
            current[1].append(line)
        else:
            preamble.append(line)
    if current:
        sections.append(current)
    return "\n".join(preamble), sections


def repair_text(text: str) -> tuple[str, int, int]:
    """Return (new_text, moved_count, dropped_duplicate_count)."""
    preamble, sections = split_sections(text)
    headers = [h.strip() for h, _ in sections]
    if "## Coverage" not in headers:
        return text, 0, 0
    cov_idx = headers.index("## Coverage")

    existing = {
        m.group(1)
        for line in sections[cov_idx][1]
        if (m := LINK_TARGET.search(line)) and ARTICLE_LINE.match(line)
    }

    moved: list[str] = []
    dropped = 0
    for i, (header, body) in enumerate(sections):
        if i == cov_idx:
            continue
        kept: list[str] = []
        for line in body:
            if not ARTICLE_LINE.match(line):
                kept.append(line)
                continue
            target = LINK_TARGET.search(line).group(1)
            if target in existing:
                dropped += 1
                continue
            existing.add(target)
            moved.append(line.strip())
        sections[i] = (header, kept)

    if not moved and not dropped:
        return text, 0, 0

    # Insert after the last existing "- " entry in the Coverage block, matching
    # patch_coverage.py's placement rule (never past the trailing blank line).
    cov_body = sections[cov_idx][1]
    last_entry = -1
    for i, line in enumerate(cov_body):
        if line.strip().startswith("- "):
            last_entry = i
    insert_at = last_entry + 1
    sections[cov_idx] = (
        sections[cov_idx][0],
        cov_body[:insert_at] + moved + cov_body[insert_at:],
    )

    rebuilt = preamble
    for header, body in sections:
        rebuilt += "\n" + header + "\n" + "\n".join(body).rstrip("\n") + "\n"
    return rebuilt.rstrip("\n") + "\n", len(moved), dropped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="Apply changes. Without this, dry run.")
    parser.add_argument("--log", action="store_true", help="Append one audit entry per repaired note to each domain log.")
    parser.add_argument("--report", type=Path, help="Write the full per-note change list to a file.")
    parser.add_argument("--domain", action="append", choices=DOMAINS, help="Restrict to one domain; repeatable.")
    args = parser.parse_args()

    domains = args.domain or DOMAINS
    results: list[tuple[str, Path, int, int]] = []

    for domain in domains:
        for path in sorted((ENTITIES / domain).glob("*.md")):
            if path.name in SYSTEM_FILES:
                continue
            text = path.read_text(encoding="utf-8")
            new_text, moved, dropped = repair_text(text)
            if moved or dropped:
                results.append((domain, path, moved, dropped))
                if args.write and new_text != text:
                    path.write_text(new_text, encoding="utf-8")

    label = "APPLIED" if args.write else "DRY RUN -- no files written"
    print(f"=== {label} ===\n")
    total_moved = sum(r[2] for r in results)
    total_dropped = sum(r[3] for r in results)
    for domain, path, moved, dropped in results[:40]:
        rel = path.relative_to(ROOT)
        extra = f", {dropped} duplicate(s) dropped" if dropped else ""
        print(f"[{domain}] {rel} -- {moved} link(s) moved into ## Coverage{extra}")
    if len(results) > 40:
        print(f"... and {len(results) - 40} more notes")

    print(
        f"\n{len(results)} notes repaired, {total_moved} links moved into ## Coverage, "
        f"{total_dropped} duplicates dropped."
    )

    if args.report:
        lines = [
            f"{domain}\t{path.relative_to(ROOT)}\t{moved}\t{dropped}"
            for domain, path, moved, dropped in results
        ]
        args.report.write_text(
            "domain\tpath\tmoved\tdropped\n" + "\n".join(lines) + "\n", encoding="utf-8"
        )
        print(f"Report written to {args.report}")

    if args.log and args.write and results:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")
        stamp = stamp[:-2] + ":" + stamp[-2:]
        by_domain: dict[str, list[tuple[Path, int, int]]] = {}
        for domain, path, moved, dropped in results:
            by_domain.setdefault(domain, []).append((path, moved, dropped))
        for domain, rows in by_domain.items():
            log_path = ENTITIES / domain / "log.md"
            if not log_path.exists():
                continue
            entries = [
                f"- {stamp} | source: stranded-Coverage repair (`scripts/repair_stranded_coverage.py`) "
                f"| entity: [[{path.stem}]] | action: moved {moved} article backlink(s) from a trailing "
                f"section into `## Coverage`{f', dropped {dropped} duplicate(s)' if dropped else ''} "
                f"| reasoning: the superseded inline append in `ingest_cascade.py` wrote backlinks to "
                f"end-of-file instead of into the Coverage block; counts were already correct and are unchanged."
                for path, moved, dropped in rows
            ]
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(entries) + "\n")
            print(f"Appended {len(entries)} entries to {log_path.relative_to(ROOT)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
