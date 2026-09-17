#!/usr/bin/env python3
"""Repair wikilinks and headings whose text contains a raw newline.

Source titles imported from the Animation Technology feed export sometimes carry an embedded
newline (typically before a closing ')' on a bilingual title). That newline was
copied verbatim into

  1. the article note's `# H1` heading, and
  2. every `[[target|Display]]` Coverage/Related-Entities label derived from it.

Obsidian wikilinks cannot span lines, so a label containing a newline renders as
broken literal text and the link stops resolving in the Outgoing Links pane.
check_links.py does not catch these because the bracket accounting still
balances.

Three mechanical, display-only repairs (targets and filenames are never
changed):

  A. NEWLINE-IN-LINK  -- inside a well-formed [[...]] with no nested '[[',
     collapse any run of whitespace containing a newline to a single space,
     then drop whitespace sitting immediately before a closing ')' and at the
     end of the label.
  B. DANGLING ')]]'   -- a line that is exactly ')]]' with no wikilink open at
     that point in the file. Residue left by topic consolidation after the
     link it belonged to was rewritten. The line is deleted.
  C. MULTILINE H1     -- an article note's '# ' heading continued onto the next
     line; the continuation is folded back onto the heading with the same
     whitespace rules as (A).

Usage:
  python3 scripts/fix_multiline_links.py --dry-run
  python3 scripts/fix_multiline_links.py
  python3 scripts/fix_multiline_links.py --domain outlet
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTITIES = ROOT / "entities"

# A well-formed link: '[[', no further '[' inside, then ']]'. DOTALL so it can
# span lines -- spanning lines is exactly what we are looking for.
LINK = re.compile(r"\[\[[^\[\]]*?\]\]", re.S)


def tidy(text: str) -> str:
    """Collapse newline-bearing whitespace and trim cosmetic padding."""
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    text = re.sub(r"\s+\)", ")", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


FENCE = re.compile(r"^```.*?^```", re.S | re.M)


def fix_links(text: str) -> tuple[str, int]:
    """Repair links outside fenced code blocks.

    ## Database Projection / ## Topic Consolidation Audit blocks are preserved
    source evidence and are never rewritten, so fenced regions are masked out
    before the link pass and restored afterwards.
    """
    count = 0
    blocks: list[str] = []

    def mask(match: re.Match) -> str:
        blocks.append(match.group(0))
        return f"\x00FENCE{len(blocks) - 1}\x00"

    masked = FENCE.sub(mask, text)

    def repl(match: re.Match) -> str:
        nonlocal count
        raw = match.group(0)
        if "\n" not in raw:
            return raw
        inner = tidy(raw[2:-2])
        count += 1
        return f"[[{inner}]]"

    masked = LINK.sub(repl, masked)
    restored = re.sub(r"\x00FENCE(\d+)\x00", lambda m: blocks[int(m.group(1))], masked)
    return restored, count


def drop_dangling(text: str) -> tuple[str, int]:
    """Delete ')]]' lines that close no open wikilink."""
    lines = text.split("\n")
    out: list[str] = []
    open_links = 0
    removed = 0
    for line in lines:
        if line.strip() == ")]]" and open_links <= 0:
            removed += 1
            continue
        out.append(line)
        open_links += line.count("[[") - line.count("]]")
        if open_links < 0:
            open_links = 0
    return "\n".join(out), removed


def fix_heading(text: str) -> tuple[str, int]:
    """Fold an article H1 that spilled onto following non-blank lines.

    Applied repeatedly: a bilingual title can spill over two continuation lines
    (title / duplicated parenthetical / lone ')'), and folding only the first
    one leaves the tail stranded.
    """
    folds = 0
    while True:
        match = re.search(r"^# (?P<head>[^\n]*)\n(?P<tail>[^\n#][^\n]*)\n", text, re.M)
        if not match:
            return text, folds
        folded = tidy(match.group("head") + "\n" + match.group("tail"))
        text = text[: match.start()] + f"# {folded}\n" + text[match.end():]
        folds += 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--domain", help="restrict to entities/<domain>/")
    args = parser.parse_args()

    base = ENTITIES / args.domain if args.domain else ENTITIES
    totals = {"files": 0, "links": 0, "dangling": 0, "headings": 0}

    for path in sorted(base.rglob("*.md")):
        # log.md is an append-only audit ledger; old entries are never rewritten.
        if path.name == "log.md":
            continue
        original = path.read_text(encoding="utf-8")
        text, dangling = drop_dangling(original)
        text, links = fix_links(text)
        headings = 0
        if path.parent.parent.name == "article" or path.parent.name == "article":
            text, headings = fix_heading(text)
        if text == original:
            continue
        totals["files"] += 1
        totals["links"] += links
        totals["dangling"] += dangling
        totals["headings"] += headings
        print(
            f"{path.relative_to(ROOT)}: links={links} dangling={dangling} heading={headings}"
        )
        if not args.dry_run:
            path.write_text(text, encoding="utf-8")

    label = "DRY RUN -- nothing written" if args.dry_run else "APPLIED"
    print(
        f"\n=== {label} === files={totals['files']} "
        f"links={totals['links']} dangling={totals['dangling']} headings={totals['headings']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
