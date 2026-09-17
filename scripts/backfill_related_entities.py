#!/usr/bin/env python3
"""Backfill a missing '## Related Entities' section in compiled article notes.

`article_quality.py --check` flags any compiled article note lacking a
`## Related Entities` heading (finding code `missing-related-section`,
severity `warning`). That check is read-only and `--fix-safe` deliberately
does not touch it — it only repairs provenance-backed ID/source-type scalar
fields, never body content (see `entities/decisions/enforce-article-quality-
gates.md`). This script is the mechanical repair for that specific class,
scoped narrowly enough to stay source-backed:

For each flagged article, it scans the article's OWN `## Summary` and
`## Key Points` prose for wikilinks that already resolve to an entity-domain
note (`people`, `organisations`, `place`, `country`, `topic`,
`appointments` — NOT `outlet`, which belongs only in `## Covered By`, and NOT
`article`, which is a duplicate-wire cross-reference, not an entity). Those
already-linked entities — decided by whoever ran the cascade, not by this
script — are surfaced into a `## Related Entities` list in the same bulleted
`[[real-filename|Display Name]]` form used everywhere else in the vault. No
new entity relationship is invented: this only re-presents links the article
body already carries.

A meaningful minority of flagged articles (mostly terse "duplicate wire
coverage of ..." stub notes that only cross-reference the fuller primary
article) have zero entity-domain links of their own — every entity they'd
carry belongs to the primary note they cite. For those, the section is added
empty (`## Related Entities` with no list under it), which satisfies the
schema honestly without fabricating a relationship the article's own text
doesn't support.

Usage:
  python3 scripts/backfill_related_entities.py --dry-run   # preview
  python3 scripts/backfill_related_entities.py --log       # apply + log
"""
import argparse
import os
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARTICLE_DIR = ROOT / "entities" / "article"
ARTICLE_LOG = ARTICLE_DIR / "log.md"
SYSTEM_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}
ENTITY_DOMAINS = {"people", "organisations", "place", "country", "topic", "appointments"}
LINK = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]]+))?\]\]")
PROSE_SECTIONS = ("Summary", "Key Points")


def build_target_domain_index():
    """Map every entity-domain note's bare stem and 'domain/stem' form -> domain."""
    target_domain = {}
    for domain in ENTITY_DOMAINS:
        domain_dir = ROOT / "entities" / domain
        if not domain_dir.is_dir():
            continue
        for path in domain_dir.glob("*.md"):
            if path.name in SYSTEM_FILES:
                continue
            stem = path.stem
            target_domain[stem] = domain
            target_domain[f"{domain}/{stem}"] = domain
    return target_domain


def sections(text):
    chunks = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    out = {}
    for i in range(1, len(chunks), 2):
        out[chunks[i].strip()] = chunks[i + 1]
    return out


def related_entities_for(text, target_domain):
    """Entity-domain wikilinks already present in this article's own Summary/Key
    Points, deduplicated, in order of first appearance. Returns [(stem, display)]."""
    secs = sections(text)
    body = "\n".join(secs.get(name, "") for name in PROSE_SECTIONS)
    seen, ordered = set(), []
    for target, disp in LINK.findall(body):
        target = target.strip()
        dom = target_domain.get(target) or target_domain.get(os.path.basename(target))
        if dom not in ENTITY_DOMAINS:
            continue
        stem = os.path.basename(target)
        if stem in seen:
            continue
        seen.add(stem)
        ordered.append((stem, disp.strip() if disp else None))
    return ordered


def missing_related_paths():
    paths = []
    for path in ARTICLE_DIR.rglob("*.md"):
        if path.name in SYSTEM_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if not re.search(r"^## Related Entities\s*$", text, re.MULTILINE):
            paths.append(path)
    return sorted(paths)


def backfill(dry_run=False, log=False):
    target_domain = build_target_domain_index()
    paths = missing_related_paths()
    changes = []  # (relpath, [(stem, display)])
    skipped = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if "## AI Context" not in text:
            skipped.append(path.relative_to(ROOT))
            continue
        entities = related_entities_for(text, target_domain)
        if entities:
            lines = "\n".join(f"- [[{stem}|{disp or stem}]]" for stem, disp in entities)
            insertion = f"## Related Entities\n{lines}\n\n"
        else:
            insertion = "## Related Entities\n\n"
        new_text = re.sub(r"\n+## AI Context", "\n\n" + insertion + "## AI Context", text, count=1)
        new_text = re.sub(
            r"(?m)^last_updated:.*$",
            f"last_updated: {datetime.now().strftime('%Y-%m-%d')}",
            new_text,
            count=1,
        )
        rel = path.relative_to(ROOT)
        changes.append((rel, entities))
        if not dry_run:
            path.write_text(new_text, encoding="utf-8")

    if log and not dry_run:
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        with ARTICLE_LOG.open("a", encoding="utf-8") as f:
            for rel, entities in changes:
                stem = rel.stem
                month = rel.parent.name
                if entities:
                    reason = (
                        f"backfilled a missing '## Related Entities' section by surfacing "
                        f"{len(entities)} entity link(s) already present in this article's own "
                        f"Summary/Key Points ({', '.join(s for s, _ in entities)}); no new entity "
                        f"relationship introduced"
                    )
                else:
                    reason = (
                        "backfilled a missing '## Related Entities' section as empty — this article's "
                        "own Summary/Key Points carry no entity-domain wikilink (a duplicate-wire stub "
                        "that only cross-references the fuller primary compiled article); no relationship "
                        "fabricated"
                    )
                f.write(
                    f"- {ts} | source: backfill-related-entities batch | "
                    f"entity: [[article/{month}/{stem}|{stem}]] | "
                    f"action: updated — added '## Related Entities' section | reasoning: {reason}.\n"
                )

    return changes, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing.")
    parser.add_argument("--log", action="store_true", help="Append one audit entry per article.")
    args = parser.parse_args()

    changes, skipped = backfill(dry_run=args.dry_run, log=args.log)
    populated = sum(1 for _, e in changes if e)
    empty = sum(1 for _, e in changes if not e)
    mode = "DRY RUN" if args.dry_run else "APPLIED"
    print(
        f"{mode}: {len(changes)} article(s) given a '## Related Entities' section "
        f"({populated} populated from existing links, {empty} left structurally empty)."
    )
    if skipped:
        print(f"Skipped {len(skipped)} (no '## AI Context' anchor found — needs manual review):")
        for p in skipped[:20]:
            print(f"  {p}")


if __name__ == "__main__":
    main()
