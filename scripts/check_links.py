#!/usr/bin/env python3
"""Vault-wide wikilink checker.

Scans every .md file for [[wikilink]] / [[target|Display]] references and reports:
  - BROKEN    — target matches no real filename and no registered alias. Always a bug.
  - NESTED    — a wikilink opened inside another still-open wikilink, e.g.
                [[1000895-[[japan|Japan]]-[[canada|Canada]]-sign|...]]. Always corruption:
                produced when an auto-linker replaces an entity name that happens to sit inside
                an existing link. Hard failure. fix_links.py's normalize pass mechanically repairs
                the *target-embedded* case (inner link before the outer '|', which breaks the
                target); the *display-embedded* case (inner link after '|', from a truncated
                ## Coverage label) is left for label regeneration, not bracket surgery.
  - UNBALANCED — a file whose count of '[[' and ']]' differ, i.e. a link missing a bracket.
                Two shapes: an isolated single-']' close (e.g. [[nisar-keshvani|Nisar Keshvani]),
                which fix_links.py repairs; and an unclosed outer link left by a truncated
                ## Coverage label whose text embeds a cross-reference (e.g.
                [[1000428-...|... as [[1000895-...]]]), whose target is still valid but which needs
                the label regenerated — reported, not auto-fixed. Hard failure either way, and
                dangerous beyond itself: an unclosed '[[' throws off bracket accounting, so a naive
                fixer can make later well-formed links look "nested" and destroy them.
  - ALIAS-ONLY — target only resolves via a note's `aliases:` frontmatter, not its filename.
                 Not strictly broken, but flagged: alias resolution proved unreliable in this
                 vault (stale Outgoing-Links-pane caching, and a single invalid YAML field
                 anywhere in a note's frontmatter silently drops its whole `aliases` list — see
                 scripts/entity_cascade_procedure.md, "Why piped links, always"). Prefer rewriting
                 these to the piped `[[real-filename|Display Name]]` form.
  - YAML ERROR — a note's frontmatter fails to parse under a real YAML parser. This is usually the
                 root cause behind ALIAS-ONLY/BROKEN results for links pointing at that note, since
                 a parse failure drops every field, not just the one that's actually malformed
                 (classic case: an unquoted '#' inside a flow list, e.g. tags: [#animation-industry]).
  - UNLINKED ENTITY — a known entity (a note's displayName/alias in people/organisations/place/
                 country/topic) appears verbatim in an article's prose (## Summary / ## Key Points)
                 but its slug is never wikilinked anywhere in that article — a missed link under the
                 vault's link-on-first-mention convention. Advisory only (a judgment call and a large
                 historical backlog); reported but never fails the exit code. Backfill with
                 fix_links.py --link-entities. Restricted to multiword names to stay high-precision;
                 outlet names are deliberately excluded (they collide with ordinary prose like "The
                 Australian helicopter", and a genuinely-covering outlet is already linked via
                 ## Covered By).

By default, skips documentation/template noise (index.md, catalog.md, log.md, _template.md,
scripts/*.md, entities/decisions/*.md) where placeholder examples and intentionally-preserved
historical log entries are expected to "fail". Pass --include-docs to check everything anyway.

Always excludes Inputs/ regardless of --include-docs: those are raw, not-yet-cascaded articles
(no entity links expected yet) rather than vault notes, and raw feed text can contain incidental
"[[...]]"-shaped text that isn't a real wikilink at all.

Usage:
  python3 scripts/check_links.py                  # check live data notes only
  python3 scripts/check_links.py --include-docs    # check every .md file, no exclusions
  python3 scripts/check_links.py --domain outlet    # restrict to entities/<domain>/

Usage (cont.):
  python3 scripts/check_links.py --no-unlinked      # skip the UNLINKED ENTITY scan (faster)
  python3 scripts/check_links.py --no-run-log       # suppress canonical run receipt

Exit code: 1 if any BROKEN link, target-embedded NESTED link, or YAML ERROR was found (these
break a link's target), 0 otherwise. Advisory findings never fail the exit code on their own:
ALIAS-ONLY, UNLINKED ENTITY, and the display-embedded NESTED / UNBALANCED "malformed Coverage
label" class (whose target still resolves — the label just needs regenerating).
"""
import sys
import os
import re
import glob
from run_logger import RunLogger

try:
    import yaml
except ImportError:
    print("This script requires PyYAML. Install it with: pip3 install pyyaml", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINK_PATTERN = re.compile(r"\[\[([^\]|#]+)")
# A single well-formed, non-nested link: [[ ... ]] with no brackets inside.
FULL_LINK = re.compile(r"\[\[[^\[\]]*\]\]")
# A '[[' opening while another '[[' is still unclosed (no ']' in between) — the
# signature of a nested/malformed link like [[a-[[b|B]]-c|X]].
NESTED_OPEN = re.compile(r"\[\[[^\]]*\[\[")
DOC_SKIP_NAMES = {"index.md", "catalog.md", "log.md", "_template.md", "README.md"}
DOC_SKIP_DIRS = ("scripts/", "entities/decisions/", "dashboards/site/node_modules/")

# Article prose sections scanned for known entities left unlinked.
PROSE_SECTIONS = ("Summary", "Key Points")
ARTICLE_PREFIX = "entities/article/"
# Entity domains whose displayName/aliases seed the UNLINKED ENTITY scan. `outlet`
# is deliberately excluded — its display names ("The Australian", "Foreign Affairs")
# collide with ordinary prose; a genuinely-covering outlet is already linked via the
# article's ## Covered By. `article` and `decisions` are not entity vocabularies.
NAME_INDEX_DOMAINS = ("people", "organisations", "place", "country", "topic")


def is_doc_file(rel_path):
    if os.path.basename(rel_path) in DOC_SKIP_NAMES:
        return True
    return any(rel_path.startswith(d) for d in DOC_SKIP_DIRS)


def link_base(content):
    """Given the text between [[ and ]], return the bare target slug (drop any
    '#anchor' and '|display')."""
    return content.split("|", 1)[0].split("#", 1)[0].strip()


def target_stem(target):
    """Normalize a wikilink target to its filename stem for entity comparisons."""
    return os.path.basename(target.strip())


def note_targets(path):
    """All wikilink targets that should resolve to this note.

    The vault prefers bare filenames, but some generated notes use Obsidian-style
    path-qualified targets such as [[topic/foo]] or [[article/2026-04/bar]].
    Treat those as resolvable when they point at an actual note on disk.
    """
    no_ext = os.path.splitext(path)[0]
    targets = {os.path.basename(no_ext), no_ext}
    if no_ext.startswith("entities/"):
        targets.add(no_ext[len("entities/"):])
    return targets


def extract_prose(text):
    """Concatenated body of the article's PROSE_SECTIONS ('## Summary', '## Key
    Points'), used to spot entity names mentioned but never linked."""
    chunks = re.split(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    out = []
    for i in range(1, len(chunks), 2):
        if chunks[i].strip() in PROSE_SECTIONS:
            out.append(chunks[i + 1])
    return "\n".join(out)


def build_entity_name_index(all_notes):
    """Map each multiword entity displayName/alias -> set of slugs, over the
    NAME_INDEX_DOMAINS. Multiword-only (>=5 chars, contains a space) keeps the
    UNLINKED ENTITY scan high-precision — short acronyms like 'US'/'Animation Industry' match
    too much ordinary text to flag safely by name alone."""
    name_to_slug = {}
    for path in all_notes:
        parts = path.split("/")
        if len(parts) < 3 or parts[0] != "entities" or parts[1] not in NAME_INDEX_DOMAINS:
            continue
        data, err = parse_frontmatter(os.path.join(ROOT, path))
        if err or not data:
            continue
        slug = os.path.splitext(os.path.basename(path))[0]
        names = []
        dn = data.get("displayName")
        if isinstance(dn, str) and dn.strip():
            names.append(dn.strip())
        aliases = data.get("aliases")
        if isinstance(aliases, list):
            names += [str(a).strip() for a in aliases]
        elif isinstance(aliases, str) and aliases.strip():
            names.append(aliases.strip())
        for n in names:
            if len(n) >= 5 and " " in n:
                name_to_slug.setdefault(n, set()).add(slug)
    return name_to_slug


def target_embedded_spots(text):
    """Offsets of inner '[[' that open inside another link's TARGET (before that
    outer link's first '|'). This is the slug-breaking corruption; walked locally
    so it's independent of any bracket imbalance elsewhere in the file."""
    spots, n, i = set(), len(text), 0
    while i < n - 1:
        if text[i] == "[" and text[i + 1] == "[":
            j = i + 2
            while j < n - 1:
                if text[j] == "|":
                    break
                if text[j] == "]" and text[j + 1] == "]":
                    break
                if text[j] == "[" and text[j + 1] == "[":
                    spots.add(j)
                    break
                j += 1
        i += 1
    return spots


def find_structural_defects(scan_notes, include_docs):
    """Classify bracket corruption by severity:
      nested_target — inner link embedded in another link's TARGET (breaks the
                      slug; the link no longer resolves). HARD failure.
      nested_display — inner link embedded in a link's DISPLAY, e.g. a truncated
                      ## Coverage label. Target still resolves; needs the label
                      regenerated. WARNING.
      unbalanced    — '[[' vs ']]' count mismatch (usually the same truncated
                      labels). WARNING.
    Returns (nested_target, nested_display, unbalanced)."""
    nested_target, nested_display, unbalanced = [], [], []
    for path in scan_notes:
        if not include_docs and is_doc_file(path):
            continue
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            text = f.read()
        te = target_embedded_spots(text)
        for m in NESTED_OPEN.finditer(text):
            snippet = text[m.start():m.start() + 70].replace("\n", " ")
            inner_pos = m.end() - 2  # the inner '[[' NESTED_OPEN just matched up to
            (nested_target if inner_pos in te else nested_display).append((path, snippet))
        n_open, n_close = text.count("[["), text.count("]]")
        if n_open != n_close:
            unbalanced.append((path, n_open, n_close))
    return nested_target, nested_display, unbalanced


def find_unlinked_entities(scan_notes, name_to_slug):
    """For each article note, known multiword entities that appear in its prose
    as plain text but whose slug is never wikilinked anywhere in the note.
    Advisory only. Returns [(path, name, slug)]."""
    findings = []
    for path in scan_notes:
        if not path.startswith(ARTICLE_PREFIX):
            continue
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            text = f.read()
        linked = {target_stem(link_base(m.group(0)[2:-2])) for m in FULL_LINK.finditer(text)}
        plain = FULL_LINK.sub("  ", extract_prose(text))
        seen = set()
        for name, slugs in name_to_slug.items():
            if slugs & linked or name in seen:
                continue
            if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", plain):
                seen.add(name)
                findings.append((path, name, sorted(slugs)[0]))
    return findings


def parse_frontmatter(path):
    """Returns (data_dict_or_None, error_or_None). data is None if there's no frontmatter at all
    (not an error — plenty of notes, e.g. index.md, are fine without any)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---"):
        return None, None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, "malformed frontmatter (no closing '---' delimiter)"
    try:
        data = yaml.safe_load(parts[1])
        return (data or {}), None
    except yaml.YAMLError as e:
        return None, str(e).splitlines()[0]


def find_all_notes(domain_filter=None):
    pattern = f"entities/{domain_filter}/**/*.md" if domain_filter else "**/*.md"
    paths = []
    for path in glob.glob(os.path.join(ROOT, pattern), recursive=True):
        if "node_modules" in path.split(os.sep):
            continue
        if os.sep + ".obsidian" + os.sep in path:
            continue
        if os.sep + "Inputs" + os.sep in path:
            continue
        paths.append(os.path.relpath(path, ROOT))
    return sorted(paths)


def main():
    args = sys.argv[1:]
    include_docs = "--include-docs" in args
    check_unlinked = "--no-unlinked" not in args
    no_run_log = "--no-run-log" in args
    domain = None
    if "--domain" in args:
        domain = args[args.index("--domain") + 1]

    run = None
    if not no_run_log:
        run = RunLogger(
            "lint",
            metadata={
                "script": "check_links.py",
                "includeDocs": include_docs,
                "checkUnlinked": check_unlinked,
                "domain": domain,
            },
        )

    all_notes = find_all_notes()
    real_targets = {target for p in all_notes for target in note_targets(p)}

    scan_notes = find_all_notes(domain_filter=domain)
    scanned_note_paths = [p for p in scan_notes if include_docs or not is_doc_file(p)]
    scanned_note_set = set(scanned_note_paths)

    alias_index = {}  # alias text -> [paths]
    yaml_errors = []  # (path, error)
    for path in all_notes:
        # Domain checks need aliases from the whole vault for correct resolution,
        # but only the selected notes need YAML validation. Most article notes do
        # not define aliases, so avoid parsing their frontmatter unnecessarily.
        if path not in scanned_note_set:
            with open(os.path.join(ROOT, path), encoding="utf-8") as f:
                header = f.read(4096)
            if (
                not re.search(r"^aliases\s*:", header, flags=re.MULTILINE)
                or re.search(r"^aliases\s*:\s*\[\]\s*$", header, flags=re.MULTILINE)
            ):
                continue
        data, err = parse_frontmatter(os.path.join(ROOT, path))
        if err and path in scanned_note_set:
            yaml_errors.append((path, err))
            continue
        if not data:
            continue
        aliases = data.get("aliases")
        if isinstance(aliases, list):
            for a in aliases:
                alias_index.setdefault(str(a), []).append(path)
        elif isinstance(aliases, str) and aliases.strip():
            alias_index.setdefault(aliases.strip(), []).append(path)

    broken = []      # (path, target)
    alias_only = []  # (path, target, resolved_paths)

    for path in scanned_note_paths:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            text = f.read()
        for m in LINK_PATTERN.finditer(text):
            target = m.group(1).strip()
            if target in real_targets:
                continue
            if target in alias_index:
                alias_only.append((path, target, alias_index[target]))
                continue
            broken.append((path, target))

    # A "broken" target containing '...' is not a missing entity — it's the
    # truncated slug of a cross-reference embedded in a ## Coverage label (real
    # slugs never contain '...'). Route these to the malformed-label advisory
    # rather than hard-failing on them; the outer link's own target still resolves.
    broken_truncated = [(p, t) for p, t in broken if "..." in t]
    broken = [(p, t) for p, t in broken if "..." not in t]

    nested_target, nested_display, unbalanced = find_structural_defects(scan_notes, include_docs)
    unlinked = []
    if check_unlinked:
        name_to_slug = build_entity_name_index(all_notes)
        unlinked = find_unlinked_entities(
            scanned_note_paths,
            name_to_slug,
        )

    print(f"Scanned {len(scan_notes)} notes"
          + (f" (domain: {domain})" if domain else "")
          + (", including docs/templates" if include_docs else ", excluding docs/templates"))
    print()

    if yaml_errors:
        print(f"YAML ERRORS ({len(yaml_errors)}):")
        for path, err in yaml_errors:
            print(f"  {path}: {err}")
        print()

    if nested_target:
        print(f"NESTED LINKS — target-embedded ({len(nested_target)}), slug broken; run fix_links.py:")
        for path, snippet in nested_target:
            print(f"  {path}: {snippet}")
        print()

    if broken:
        print(f"BROKEN LINKS ({len(broken)}):")
        for path, target in broken:
            print(f"  {path}: [[{target}]] -> no file and no alias match")
        print()

    if nested_display or unbalanced or broken_truncated:
        affected = sorted({p for p, _ in nested_display}
                          | {p for p, _, _ in unbalanced}
                          | {p for p, _ in broken_truncated})
        print(f"MALFORMED COVERAGE LABELS ({len(affected)} notes) — target resolves but a link is "
              f"embedded in a truncated ## Coverage label; regenerate the label (advisory):")
        for path in affected[:15]:
            print(f"  {path}")
        if len(affected) > 15:
            print(f"  ... and {len(affected) - 15} more")
        print()

    if alias_only:
        print(f"ALIAS-ONLY LINKS ({len(alias_only)}) — resolves, but fragile; prefer piped form:")
        for path, target, resolved in alias_only:
            print(f"  {path}: [[{target}]] -> resolves via alias only, to {resolved[0]}")
        print()

    if unlinked:
        n_articles = len({p for p, _, _ in unlinked})
        print(f"UNLINKED ENTITIES ({len(unlinked)} across {n_articles} articles) — advisory; "
              f"backfill with fix_links.py --link-entities:")
        for path, name, slug in unlinked[:15]:
            print(f"  {path}: \"{name}\" mentioned in prose but [[{slug}]] never linked")
        if len(unlinked) > 15:
            print(f"  ... and {len(unlinked) - 15} more")
        print()

    hard_fail = bool(broken or yaml_errors or nested_target)
    warnings = bool(nested_display or unbalanced or broken_truncated or alias_only or unlinked)
    if not (hard_fail or warnings):
        print("CLEAN: no broken, nested, or alias-only links, no YAML errors, "
              "no malformed labels, no unlinked entities.")

    if run:
        article_count = len([p for p in scanned_note_paths if p.startswith(ARTICLE_PREFIX)])
        warning_count = (
            len(nested_display)
            + len(unbalanced)
            + len(broken_truncated)
            + len(alias_only)
            + len(unlinked)
        )
        run.status = "failed" if hard_fail else "ok"
        run.set_article_metrics(
            inputCount=article_count,
            processedCount=article_count,
            failedCount=len(broken) + len(yaml_errors) + len(nested_target),
        )
        run.set_file_metrics(
            scannedCount=len(scanned_note_paths),
            failedCount=len(broken) + len(yaml_errors) + len(nested_target),
        )
        run.add_output("brokenLinks", len(broken))
        run.add_output("yamlErrors", len(yaml_errors))
        run.add_output("nestedTargetLinks", len(nested_target))
        run.add_output("warnings", warning_count)
        run.add_output("aliasOnlyLinks", len(alias_only))
        run.add_output("unlinkedEntityMentions", len(unlinked))
        run.finish()

    sys.exit(1 if hard_fail else 0)


if __name__ == "__main__":
    main()
