#!/usr/bin/env python3
"""Repair malformed entity Coverage labels from the cited article summary.

Only rewrites Coverage lines whose display label contains an embedded wikilink.
The outer article target is retained exactly; the new label is derived from that
article's own Summary, so no external enrichment or link-target guessing occurs.
"""
import argparse
import re
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
ENTITY_DOMAINS = ("country", "organisations", "people", "place", "topic", "outlet")
MALFORMED_LINE = re.compile(r"^(\s*- \[\[([^\[\]|]+)\|.*\[\[.*)$")
WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def article_paths():
    """Return compiled article notes keyed by every target form Coverage links use.

    Coverage links usually store the bare article stem as their target, but some
    (see check_links.py's note_targets(), which treats both forms as resolvable)
    use the Obsidian-style path-qualified form article/<month>/<stem>. Index both
    so this repair pass can resolve either without changing the target itself.
    """
    index: dict[str, Path] = {}
    for path in (ROOT / "entities" / "article").rglob("*.md"):
        if path.name in {"index.md", "catalog.md", "log.md"}:
            continue
        index[path.stem] = path
        qualified = path.relative_to(ROOT / "entities").with_suffix("").as_posix()
        index[qualified] = path
    return index


def summary_label(article_path):
    """Derive a replacement Coverage label from one article's Summary.

    Existing wikilinks are flattened to display text before the first sentence
    is truncated. This keeps the repair source-backed and prevents nested links
    from being reintroduced into the entity Coverage list.
    """
    text = article_path.read_text(encoding="utf-8")
    match = re.search(r"^## Summary\s*$\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match:
        return None
    summary = re.sub(r"\s+", " ", match.group(1)).strip()
    summary = WIKILINK.sub(lambda m: m.group(2) or m.group(1), summary)
    summary = summary.replace("[[", "").replace("]]", "").replace("|", " ")
    if not summary:
        return None
    # Split on sentence-ending '.'/'?' only. Several outlet names in this vault's
    # corpus end in '!' ("Yahoo!", "tabla!"), so treating '!' as a sentence
    # boundary truncates the label to just the outlet name whenever a Summary
    # opens with an attribution like "Yahoo! News Hong Kong (7 Feb) reported...".
    # Purely mechanical -- still the article's own first sentence, just measured
    # correctly.
    first_sentence = re.split(r"(?<=[.?])\s+", summary, maxsplit=1)[0]
    return first_sentence[:180].rstrip()


def regenerate(dry_run=False, log=False, domains=None):
    """Return (changed_links, changed_files, skipped) after an optional repair.

    Only lines with embedded wikilinks in the display label are eligible. The
    outer article target is preserved exactly, which is the core safety
    property of this utility.
    """
    articles = article_paths()
    changed_files = 0
    changed_links = 0
    skipped = []
    changes = []

    active_domains = ENTITY_DOMAINS if domains is None else tuple(
        domain for domain in domains if domain in ENTITY_DOMAINS
    )
    for domain in active_domains:
        for path in sorted((ROOT / "entities" / domain).glob("*.md")):
            if path.name in {"index.md", "catalog.md", "log.md"}:
                continue
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            changed = False
            rebuilt = []
            for line in lines:
                match = MALFORMED_LINE.match(line.rstrip("\n"))
                if not match:
                    rebuilt.append(line)
                    continue
                target = match.group(2).strip()
                # If the cited article cannot provide a summary, leave the
                # line unchanged and report it for manual review.
                label = summary_label(articles[target]) if target in articles else None
                if not label:
                    skipped.append((path.relative_to(ROOT), target))
                    rebuilt.append(line)
                    continue
                rebuilt.append(f"- [[{target}|{label}]]\n")
                changed = True
                changed_links += 1
            if changed:
                changed_files += 1
                changes.append((domain, path.stem, target, label))
                if not dry_run:
                    path.write_text("".join(rebuilt), encoding="utf-8")

    if log and not dry_run:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        for domain, entity_id, source_id, source_label in changes:
            log_path = ROOT / "entities" / domain / "log.md"
            line = (
                f"- {timestamp} | source: [[{source_id}|{source_label}]] | "
                f"entity: [[{entity_id}]] | action: updated — regenerated malformed Coverage label(s) | "
                "reasoning: repaired target-resolving labels that embedded truncated wikilinks; "
                "new label derived solely from the cited article's Summary.\n"
            )
            with log_path.open("a", encoding="utf-8") as log:
                log.write(line)

    return changed_links, changed_files, skipped


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", action="store_true", help="append one audit entry per changed entity note")
    args = parser.parse_args()

    changed_links, changed_files, skipped = regenerate(args.dry_run, args.log)

    mode = "DRY RUN" if args.dry_run else "APPLIED"
    print(f"{mode}: {changed_links} Coverage labels regenerated in {changed_files} entity notes.")
    if skipped:
        print(f"Skipped {len(skipped)} labels with no usable cited-article Summary:")
        for path, target in skipped[:20]:
            print(f"  {path}: {target}")
        if len(skipped) > 20:
            print(f"  ... and {len(skipped) - 20} more")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
