#!/usr/bin/env python3
"""Link orphan entity notes to explicitly named peer entities in their bodies.

An orphan here is an entity note with no outgoing link to another entity note.
Only the prose body is considered: YAML, Coverage, Source, and AI Context are
excluded. Matches are deliberately conservative: multiword names and uppercase
acronyms only, never the note's own name, and never text already inside a link.
"""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
ENTITY_DOMAINS = ("appointments", "country", "issues", "organisations", "outlet", "people", "place", "topic")
SKIP_FILES = {"index.md", "catalog.md", "log.md", "_template.md"}
SKIP_SECTIONS = {"Coverage", "AI Context", "Source", "Resolver Note"}
LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]*)?\]\]")


def split_frontmatter(text):
    """Return parsed frontmatter plus body text without mutating the note.

    Invalid or absent frontmatter is treated as empty metadata because this
    script only needs display surfaces from well-formed peer notes; malformed
    files are better surfaced by check_links.py/fix_links.py.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    data = yaml.safe_load(text[3:end]) or {}
    return data, text[end + 4:]


def entity_files():
    """Map every entity slug in the editable domains to its domain and path."""
    files = {}
    for domain in ENTITY_DOMAINS:
        for path in (ROOT / "entities" / domain).glob("*.md"):
            if path.name not in SKIP_FILES:
                files[path.stem] = (domain, path)
    return files


def is_matchable(surface):
    """Keep only high-signal names safe enough for mechanical body linking."""
    return (
        len(surface) >= 5 and " " in surface
    ) or (len(surface) >= 3 and surface.isupper())


def entity_surfaces(files):
    """Build an unambiguous surface-name index for peer-entity matching.

    A display name or alias is usable only if it points to exactly one slug.
    Ambiguous names are deliberately dropped rather than guessed, because this
    pass writes links into prose.
    """
    candidates = {}
    for slug, (_domain, path) in files.items():
        data, _body = split_frontmatter(path.read_text(encoding="utf-8"))
        values = [data.get("displayName"), *(data.get("aliases") or []), *(data.get("acronyms") or [])]
        for value in values:
            if isinstance(value, str) and is_matchable(value.strip()):
                candidates.setdefault(value.strip(), set()).add(slug)
    # Do not guess when a surface name maps to more than one entity.
    surfaces = [(surface, next(iter(slugs))) for surface, slugs in candidates.items() if len(slugs) == 1]
    surfaces.sort(key=lambda item: len(item[0]), reverse=True)
    pattern = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(surface) for surface, _slug in surfaces) + r")(?!\w)")
    return {surface: slug for surface, slug in surfaces}, pattern


def section_spans(body):
    """Return body ranges where relationship links may be added.

    Coverage, provenance, resolver, and AI-context sections are excluded
    because they either contain generated backlinks or operational notes rather
    than narrative evidence of a peer relationship.
    """
    headers = [(match.group(1).strip(), match.start(), match.end())
               for match in re.finditer(r"^##\s+(.+)$", body, re.MULTILINE)]
    spans = []
    for index, (header, _start, end) in enumerate(headers):
        next_start = headers[index + 1][1] if index + 1 < len(headers) else len(body)
        if header not in SKIP_SECTIONS:
            spans.append((end, next_start))
    return spans or [(0, len(body))]


def linked_entity_targets(body, files, own_slug):
    """Return existing outgoing entity links, excluding self-links."""
    targets = set()
    for match in LINK.finditer(body):
        target = match.group(1).strip()
        if target in files and target != own_slug:
            targets.add(target)
    return targets


def link_orphan_body(body, own_slug, files, surface_map, surface_pattern):
    """Link explicit peer-entity mentions only when the note is an orphan.

    The function works against masked text so HTML template comments and
    existing wikilinks cannot trigger new links. Insertions are applied from the
    end of the body backward to keep earlier offsets stable.
    """
    if linked_entity_targets(body, files, own_slug):
        return body, []

    # Template guidance in HTML comments is not evidence of a relationship.
    masked_body = re.sub(r"<!--.*?-->", lambda match: " " * len(match.group(0)), body, flags=re.DOTALL)
    occupied = [(match.start(), match.end()) for match in LINK.finditer(body)]
    accepted = []
    accepted_slugs = set()
    for start, end in section_spans(masked_body):
        for match in surface_pattern.finditer(masked_body, start, end):
            surface = match.group(0)
            slug = surface_map[surface]
            if slug == own_slug or slug in accepted_slugs:
                continue
            if (match.start() > 0 and body[match.start() - 1] == "[") or \
               (match.end() < len(body) and body[match.end()] == "]"):
                continue
            if any(match.start() < used_end and used_start < match.end() for used_start, used_end in occupied):
                continue
            accepted.append((match.start(), match.end(), slug, surface))
            accepted_slugs.add(slug)
            occupied.append((match.start(), match.end()))

    for start, end, slug, surface in sorted(accepted, reverse=True):
        body = body[:start] + f"[[{slug}|{surface}]]" + body[end:]
    return body, [(slug, surface) for _start, _end, slug, surface in sorted(accepted)]


def link_orphans(dry_run=False, log=False, domains=None):
    """Apply orphan-body linking across selected domains and return changes."""
    files = entity_files()
    surface_map, surface_pattern = entity_surfaces(files)
    active_domains = ENTITY_DOMAINS if domains is None else tuple(
        domain for domain in domains if domain in ENTITY_DOMAINS
    )
    changes = []

    for slug, (domain, path) in files.items():
        if domain not in active_domains:
            continue
        text = path.read_text(encoding="utf-8")
        _data, body = split_frontmatter(text)
        new_body, added = link_orphan_body(body, slug, files, surface_map, surface_pattern)
        if not added:
            continue
        changes.append((domain, slug, added))
        if not dry_run:
            prefix = text[:len(text) - len(body)]
            path.write_text(prefix + new_body, encoding="utf-8")

    if log and not dry_run:
        timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        for domain, slug, added in changes:
            links = ", ".join(f"[[{target}|{surface}]]" for target, surface in added)
            with (ROOT / "entities" / domain / "log.md").open("a", encoding="utf-8") as ledger:
                ledger.write(
                    f"- {timestamp} | source: link-lint auto-fix | entity: [[{slug}]] | "
                    f"action: updated — added {len(added)} entity-body wikilink(s): {links} | "
                    "reasoning: note had no outgoing entity relationship and named these peer entities in its prose.\n"
                )

    return changes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--report", help="write the full change list as JSON")
    parser.add_argument("--max-report", type=int, default=30, help="maximum change rows printed")
    args = parser.parse_args()
    changes = link_orphans(args.dry_run, args.log)
    if args.report:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = ROOT / report_path
        report_path.write_text(
            json.dumps(
                [
                    {"domain": domain, "entity": slug, "links": len(added), "targets": [target for target, _ in added]}
                    for domain, slug, added in changes
                ],
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    print(f"{'DRY RUN' if args.dry_run else 'APPLIED'}: {sum(len(added) for _, _, added in changes)} "
          f"entity-body links added across {len(changes)} orphan entity notes.")
    for domain, slug, added in changes[:args.max_report]:
        print(f"  {domain}/{slug}: {len(added)}")
    if len(changes) > args.max_report:
        print(f"  ... and {len(changes) - args.max_report} more (full list: {args.report})")


if __name__ == "__main__":
    main()
