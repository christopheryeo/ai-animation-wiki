#!/usr/bin/env python3
"""Vault-wide wikilink lint + auto-fix.

Extends check_links.py's detection with safe, mechanical repairs.

Pass 0 (normalize) runs FIRST, before everything else, and is always on
(disable with --skip-normalize). It repairs the two STRUCTURAL corruption
classes check_links.py flags as hard failures:

  0a. TARGET-EMBEDDED NESTED links -- an inner [[slug|Display]] that got
      embedded inside another link's *target*, e.g.
        [[1000895-[[japan|Japan]]-[[canada|Canada]]-sign|...]]
      is collapsed back to [[1000895-japan-canada-sign|...]] by rewriting
      each nested inner link to its bare slug, to a fixed point. Decided
      locally per link (does an inner '[[' open before this link's '|'?),
      never by global '[[' vs ']]' counting -- global counting lets one
      malformed link elsewhere in a file make a valid standalone link look
      nested and get destroyed (a real bug this pass was rewritten to avoid).
  0b. ISOLATED single-']' closes -- a link closed with one ']' , e.g.
        [[nisar-keshvani|Nisar Keshvani]
      gets its missing bracket restored.

  An inner link embedded in another link's DISPLAY (after '|') is not changed
  by Pass 0. It is handled later by the Coverage-label pass, which retains the
  valid outer target and regenerates its label from the cited article Summary.

  Why first: the entity linker below reasons about what text is "already
  inside a link". Target-embedded corruption must be gone before it runs.

Then the three "stale or broken link" classes that don't require judgment:

  1. ALIAS-ONLY links -- rewritten in place to the canonical piped form
     [[real-filename|Display]], preserving whatever text was already
     displayed (an existing |label if piped, otherwise the original
     bracketed text). This never changes what a reader sees, only what
     the link resolves against -- see entity_cascade_procedure.md, "Why
     piped links, always".

  2. BROKEN article links whose target is already a fully-compiled note
     sitting in Inputs/articles/<month>/ (frontmatter starts `type:
     source`, has a `sourceId` field, has a `## Summary` section) --
     relocated into entities/article/<month>/ under the same filename,
     entities/article/catalog.md regenerated, and a backfilled log.md
     entry appended (one line per article, never batched). This is the
     "compiled but never moved" pattern found repeatedly in this vault
     (see entities/article/log.md's 2026-07-07 backfill entries) --
     relocating one article can surface more of the same (an article's
     own body cites further pending articles), so this step repeats to
     a fixed point.

  3. Two known YAML frontmatter corruption patterns, repaired if and
     only if applying the fix makes the frontmatter parse validly under
     PyYAML (never applied speculatively, never kept if it doesn't):
       - unquoted '#' as the first character of a flow-list item, e.g.
         tags: [#animation-industry] -> tags: ['#animation-industry']
       - a backslash-escaped apostrophe inside a single-quoted flow
         scalar, e.g. aliases: ['Women\\'s Institute'] ->
         aliases: ["Women's Institute"]

  4. MALFORMED COVERAGE LABELS -- a valid article target whose display label
     embeds a truncated wikilink is rebuilt from that cited article's own
     `## Summary`. The target, source note and entity counts are untouched.
     Each changed entity receives an append-only audit entry. Disable only
     this pass with `--skip-coverage-labels`.

  5. ORPHAN ENTITY BODIES -- an entity note with no outgoing link to another
     entity is scanned for explicitly named peer entities in its prose. YAML,
     Coverage, Source, AI Context, template comments, existing links, and the
     note's own name are excluded. Only unambiguous multiword names and
     uppercase acronyms are linked. Disable only this pass with
     `--skip-entity-body-links`.

Pass 6 (--link-entities) is OPT-IN, off by default, and runs LAST. It
backfills inline links in article prose: for each article note it finds the
first plain-text mention of a known entity in ## Summary / ## Key Points and
wraps it in a piped link, using the article's own ## Related Entities and
## Covered By as the trusted vocabulary, augmented by multiword displayName/
alias matches from people/organisations/place/country/topic (outlet excluded
from augmentation -- its names collide with ordinary prose; a covering outlet
is still linked when it's in this article's ## Covered By). First mention only
(the vault links on first mention, then uses plain text), longest name first,
never inside an existing link. This is the one pass that edits prose content
rather than just link syntax, which is why it is opt-in rather than default;
it depends on Pass 0 having already run, so it is never allowed before it.

Anything left after these passes needs a judgment call and is only
reported, never guessed at:
  - BROKEN links with no match anywhere (entities/ or Inputs/articles/)
    -- the referenced entity plain doesn't exist yet; create it via
    entity_cascade_procedure.md, don't invent a stub here.
  - YAML errors that don't match either known pattern above.
  - UNLINKED ENTITY warnings when --link-entities was not passed (run it to
    backfill), or entities whose display name never appears verbatim in prose.

Like check_links.py, this always excludes documentation/template files
(index.md, catalog.md, log.md, _template.md, scripts/*.md,
entities/decisions/*.md) and Inputs/ -- those are never auto-fixed, only
ever reported if --include-docs is passed (report-only; still never
written to).

Usage:
  python3 scripts/fix_links.py                  # scan, fix, report
  python3 scripts/fix_links.py --dry-run        # report only, write nothing
  python3 scripts/fix_links.py --domain outlet  # restrict alias/YAML passes
                                                   to entities/outlet/ (article
                                                   relocation is always
                                                   vault-wide, since it closes
                                                   the Inputs -> entities/article
                                                   gap regardless of who's
                                                   linking to it)
  python3 scripts/fix_links.py --skip-relocate  # skip pass 2
  python3 scripts/fix_links.py --skip-alias     # skip pass 1
  python3 scripts/fix_links.py --skip-yaml      # skip pass 3
  python3 scripts/fix_links.py --skip-coverage-labels # skip pass 4
  python3 scripts/fix_links.py --skip-entity-body-links # skip pass 5
  python3 scripts/fix_links.py --skip-normalize # skip pass 0 (structural repair)
  python3 scripts/fix_links.py --link-entities  # also run pass 6 (backfill article prose links)
  python3 scripts/fix_links.py --no-run-log     # suppress canonical run receipt

Run this regularly -- after any ingest/cascade batch, or on a schedule --
to keep link rot from accumulating. Exit code 1 if anything is left
requiring manual attention, 0 if clean. Requires PyYAML.
"""
import sys
import os
import re
import glob
import shutil
import subprocess
from datetime import datetime
from regenerate_coverage_labels import regenerate
from link_orphan_entity_bodies import link_orphans
from run_logger import RunLogger

try:
    import yaml
except ImportError:
    print("This script requires PyYAML. Install it with: pip3 install pyyaml", file=sys.stderr)
    sys.exit(2)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTITIES_DIR = os.path.join(ROOT, "entities")
INPUTS_ARTICLES_DIR = os.path.join(ROOT, "Inputs", "articles")
LINK_PATTERN = re.compile(r"\[\[([^\]]+)\]\]")
DOC_SKIP_NAMES = {"index.md", "catalog.md", "log.md", "_template.md", "README.md"}
DOC_SKIP_DIRS = ("scripts/", "entities/decisions/", "dashboards/site/node_modules/")

# --- Pass 0 (normalize) ---
# A single well-formed, non-nested link.
FULL_LINK = re.compile(r"\[\[[^\[\]]*\]\]")
# An inner link to collapse to its bare slug when nested inside another link's TARGET.
INNER_LINK = re.compile(r"\[\[([^\[\]|]+)\|[^\[\]]+\]\]")
INNER_LINK_NOPIPE = re.compile(r"\[\[([^\[\]|]+)\]\]")
# A link closed with a single ']' (not followed by another ']') and no inner brackets.
SINGLE_CLOSE = re.compile(r"\[\[([^\[\]]+)\](?!\])")

# --- Pass 4 (--link-entities) ---
PROSE_SECTIONS = ("Summary", "Key Points")
ARTICLE_PREFIX = "entities/article/"
# All domains whose notes are linkable entities (used to match a trusted slug's
# surface forms in prose). outlet IS included here so a covering outlet named in
# ## Covered By can still be linked.
LINKABLE_DOMAINS = ("people", "organisations", "place", "country", "topic", "outlet")
# Domains that seed the *global* multiword augmentation (entities not already in
# the article's Related Entities/Covered By). outlet excluded -- its names collide
# with ordinary prose ("The Australian helicopter").
AUGMENT_DOMAINS = ("people", "organisations", "place", "country", "topic")


def is_doc_file(rel_path):
    if os.path.basename(rel_path) in DOC_SKIP_NAMES:
        return True
    return any(rel_path.startswith(d) for d in DOC_SKIP_DIRS)


def _collapse_target_embedded(text):
    """Collapse ONE inner link that opens inside an outer link's TARGET (before
    the outer's first '|') to its bare slug. Returns new text, or None if there
    is none.

    This is the exact shape of the auto-linker corruption -- an entity name
    replaced inside an existing link's slug, e.g. [[msn-[[malaysia|Malaysia]]|
    MSN Malaysia]] or [[1000895-[[japan|Japan]]-[[canada|Canada]]-sign|...]].
    Nesting is decided LOCALLY by walking each outer link's target, with no
    global '[[' vs ']]' counting -- so one malformed link elsewhere in the file
    can never make a valid standalone link look nested and get destroyed (the
    bug that global counting caused). Inner links that appear in a link's
    DISPLAY (after '|') are deliberately left alone: those are truncated
    ## Coverage labels that need label regeneration, not bracket surgery, and
    rewriting them here would corrupt the still-valid outer link."""
    n = len(text)
    i = 0
    while i < n - 1:
        if text[i] == "[" and text[i + 1] == "[":
            j = i + 2
            while j < n - 1:
                if text[j] == "|":
                    break                      # into the display; target had no embedded link
                if text[j] == "]" and text[j + 1] == "]":
                    break                      # target closed (bare link, no pipe)
                if text[j] == "[" and text[j + 1] == "[":
                    inner = INNER_LINK.match(text, j) or INNER_LINK_NOPIPE.match(text, j)
                    if inner:
                        return text[:j] + inner.group(1) + text[inner.end():]
                    break                      # inner we can't safely collapse; leave for report
                j += 1
        i += 1
    return None


def normalize_links(text):
    """Repair target-embedded NESTED links and isolated single-']' closes, to a
    fixed point. Returns (new_text, changed). Non-destructive by construction:
    display-embedded nesting and other unbalanced-bracket cases are left for
    check_links to report, since those need judgment (label regeneration), not
    mechanical repair. Single-close and one target collapse are interleaved and
    repeated until stable, because collapsing a nest can expose a single ']'."""
    original = text
    while True:
        before = text
        # Coverage labels can contain bracketed article-title prefixes like
        # [Southeast Asia Airshow]. If a linker wraps the inner words, it creates
        # triple brackets inside the outer article link display. Preserve the
        # visible label text and remove the inner wikilink markup.
        text = re.sub(r"\[\[\[([^\[\]|]+)\|([^\[\]]+)\]\]\]", r"[\2]", text)
        text = re.sub(r"\[\[\[([^\[\]|]+)\|([^\[\]]+)\]\]\s+([^\[\]]+)\]", r"[\2 \3]", text)
        text = SINGLE_CLOSE.sub(lambda m: f"[[{m.group(1)}]]", text)
        nxt = _collapse_target_embedded(text)
        if nxt is not None:
            text = nxt
        if text == before:
            break
    return text, (text != original)


def section_body_spans(text):
    """Map each '## Header' to a list of (start, end) offsets covering its body."""
    heads = [(m.group(1).strip(), m.start(), m.end())
             for m in re.finditer(r"^##\s+(.+)$", text, flags=re.MULTILINE)]
    spans = {}
    for i, (h, _hs, he) in enumerate(heads):
        end = heads[i + 1][1] if i + 1 < len(heads) else len(text)
        spans.setdefault(h, []).append((he, end))
    return spans


def article_related_slugs(text):
    """Slugs the article already declares in ## Related Entities / ## Covered By --
    the trusted vocabulary for linking its prose."""
    slugs = set()
    for header, spans in section_body_spans(text).items():
        if header not in ("Related Entities", "Covered By"):
            continue
        for s, e in spans:
            for m in FULL_LINK.finditer(text[s:e]):
                target = m.group(0)[2:-2].split("|", 1)[0].split("#", 1)[0].strip()
                slugs.add(os.path.basename(target))
    return slugs


def build_entity_indexes():
    """Returns (slug_names, name_to_slug):
      slug_names   -- slug -> set(surface forms), over LINKABLE_DOMAINS (incl. outlet)
      name_to_slug -- multiword surface -> set(slugs), over AUGMENT_DOMAINS (no outlet)
    """
    slug_names = {}
    name_to_slug = {}
    for domain in LINKABLE_DOMAINS:
        for full in glob.glob(os.path.join(ENTITIES_DIR, domain, "*.md")):
            slug = os.path.splitext(os.path.basename(full))[0]
            with open(full, encoding="utf-8") as f:
                fm, _, ok = split_frontmatter(f.read())
            if not ok:
                continue
            try:
                data = yaml.safe_load(fm) or {}
            except yaml.YAMLError:
                continue
            names = set()
            dn = data.get("displayName")
            if isinstance(dn, str) and dn.strip():
                names.add(dn.strip())
            aliases = data.get("aliases")
            if isinstance(aliases, list):
                names |= {str(a).strip() for a in aliases if str(a).strip()}
            elif isinstance(aliases, str) and aliases.strip():
                names.add(aliases.strip())
            names = {n for n in names if len(n) >= 3}
            if names:
                slug_names.setdefault(slug, set()).update(names)
            if domain in AUGMENT_DOMAINS:
                for n in names:
                    if len(n) >= 5 and " " in n:
                        name_to_slug.setdefault(n, set()).add(slug)
    return slug_names, name_to_slug


def link_entities_in_article(text, slug_names, name_to_slug):
    """Backfill first-mention inline links in the article's prose sections.
    Returns (new_text, added) where added is [(surface, slug)]."""
    prose_spans = section_body_spans(text).get("Summary", []) + \
        section_body_spans(text).get("Key Points", [])
    if not prose_spans:
        return text, []

    cand = {s for s in article_related_slugs(text) if s in slug_names}
    prose_plain = FULL_LINK.sub("  ", "".join(text[s:e] for s, e in prose_spans))
    for name, slugs in name_to_slug.items():
        if re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", prose_plain):
            cand |= slugs

    # Link-on-first-mention: if an entity is already linked ANYWHERE in the note,
    # leave it — never add a second link to a later plain mention. (Without this,
    # linking the first *unlinked* occurrence would wrongly wrap every entity's
    # second mention on articles that already link the first.)
    linked_anywhere = {os.path.basename(m.group(0)[2:-2].split("|", 1)[0].split("#", 1)[0].strip())
                       for m in FULL_LINK.finditer(text)}
    cand -= linked_anywhere

    surfaces = sorted(((surf, sl) for sl in cand for surf in slug_names.get(sl, ())),
                      key=lambda x: -len(x[0]))
    occupied = [(m.start(), m.end()) for m in FULL_LINK.finditer(text)]

    def overlaps(s, e):
        return any(s < oe and os_ < e for os_, oe in occupied)

    accepted = []   # (start, end, slug, surface)
    used = set()
    for surface, sl in surfaces:
        if sl in used:
            continue
        pat = re.compile(r"(?<!\w)" + re.escape(surface) + r"(?!\w)")
        hit = None
        for s, e in prose_spans:
            for m in pat.finditer(text, s, e):
                # Avoid turning existing bracketed labels like [NDP Media Feature]
                # into malformed triple-bracket links.
                if (m.start() > 0 and text[m.start() - 1] == "[") or \
                   (m.end() < len(text) and text[m.end()] == "]"):
                    continue
                if not overlaps(m.start(), m.end()):
                    hit = (m.start(), m.end())
                    break
            if hit:
                break
        if hit:
            accepted.append((hit[0], hit[1], sl, surface))
            occupied.append(hit)
            used.add(sl)

    if not accepted:
        return text, []
    for s, e, sl, surface in sorted(accepted, key=lambda x: -x[0]):
        text = text[:s] + f"[[{sl}|{surface}]]" + text[e:]
    return text, [(surface, sl) for _s, _e, sl, surface in sorted(accepted, key=lambda x: x[0])]


def find_all_notes(domain_filter=None):
    pattern = f"entities/{domain_filter}/**/*.md" if domain_filter else "**/*.md"
    paths = []
    for path in glob.glob(os.path.join(ROOT, pattern), recursive=True):
        if os.sep + ".obsidian" + os.sep in path:
            continue
        if os.sep + "Inputs" + os.sep in path:
            continue
        paths.append(os.path.relpath(path, ROOT))
    return sorted(paths)


def split_frontmatter(text):
    """Returns (frontmatter_yaml, body, ok) where full text reconstructs as
    '---' + frontmatter_yaml + '---' + body. ok is False if there's no
    frontmatter block to split (not necessarily an error -- some notes,
    e.g. index.md, are fine without one)."""
    if not text.startswith("---"):
        return None, None, False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None, False
    return parts[1], parts[2], True


def try_parse(frontmatter_yaml):
    try:
        yaml.safe_load(frontmatter_yaml)
        return True
    except yaml.YAMLError:
        return False


def fix_backslash_escaped_quote(fm):
    def repl(m):
        inner = m.group(1)
        if "\\'" not in inner:
            return m.group(0)
        return '"' + inner.replace("\\'", "'").replace('"', '\\"') + '"'
    return re.sub(r"'((?:[^'\\]|\\.)*)'", repl, fm)


def fix_unquoted_hash_in_list(fm):
    """Operates via re.sub directly on the full text (never split-by-line
    and rejoin) so every original newline is preserved exactly -- splitting
    on '\\n' and rejoining with '\\n'.join() silently drops a trailing
    newline, which corrupted output the first time this was tried."""
    def fix_matched_line(m):
        line = m.group(0)
        return re.sub(r"([\[,]\s*)(#[^,\]']+)", lambda mm: f"{mm.group(1)}'{mm.group(2)}'", line)
    return re.sub(r"^([^\n:#]+:\s*\[[^\]\n]*#[^\]\n]*\])", fix_matched_line, fm, flags=re.MULTILINE)


def try_fix_yaml_error(fm_text):
    """Try known corruption patterns, one at a time, re-validating after
    each. Returns (fixed_text, patterns_applied) or (None, []) if nothing
    got it to parse."""
    applied = []
    candidate = fm_text
    for name, fn in (
        ("backslash-escaped apostrophe in single-quoted scalar", fix_backslash_escaped_quote),
        ("unquoted '#' in flow list", fix_unquoted_hash_in_list),
    ):
        attempt = fn(candidate)
        if attempt != candidate:
            candidate = attempt
            applied.append(name)
            if try_parse(candidate):
                return candidate, applied
    return (candidate, applied) if applied and try_parse(candidate) else (None, [])


def note_targets(path):
    no_ext = os.path.splitext(path)[0]
    targets = {os.path.basename(no_ext), no_ext}
    if no_ext.startswith("entities/"):
        targets.add(no_ext[len("entities/"):])
    return targets


def build_context(all_notes):
    real_targets = {target for p in all_notes for target in note_targets(p)}
    alias_index = {}
    yaml_errors = {}
    for path in all_notes:
        full = os.path.join(ROOT, path)
        with open(full, encoding="utf-8") as f:
            text = f.read()
        fm, _, ok = split_frontmatter(text)
        if not ok:
            continue
        try:
            data = yaml.safe_load(fm) or {}
        except yaml.YAMLError as e:
            yaml_errors[path] = str(e).splitlines()[0]
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        aliases = data.get("aliases")
        if isinstance(aliases, list):
            for a in aliases:
                alias_index.setdefault(str(a), []).append(stem)
        elif isinstance(aliases, str) and aliases.strip():
            alias_index.setdefault(aliases.strip(), []).append(stem)
    return real_targets, alias_index, yaml_errors


def split_link_target(content):
    """content is whatever was between [[ and ]]. Returns (base_target,
    anchor, label) -- anchor is '#Section' if present (Obsidian heading
    link), label is the text after a literal | if piped, else None."""
    if "|" in content:
        target_part, label = content.split("|", 1)
    else:
        target_part, label = content, None
    if "#" in target_part:
        base, anchor = target_part.split("#", 1)
        anchor = "#" + anchor
    else:
        base, anchor = target_part, ""
    return base, anchor, label


def rewrite_alias_only_links(text, real_targets, alias_index, report, rel_path):
    changed = False

    def repl(m):
        nonlocal changed
        content = m.group(1)
        base, anchor, label = split_link_target(content)
        if base in real_targets or base not in alias_index:
            return m.group(0)
        real = alias_index[base][0]
        display = label if label is not None else (base + anchor)
        changed = True
        report.append((rel_path, content, f"{real}{anchor}|{display}"))
        return f"[[{real}{anchor}|{display}]]"

    new_text = LINK_PATTERN.sub(repl, text)
    return new_text, changed


def find_broken_targets(all_notes, real_targets, alias_index):
    broken = []
    for path in all_notes:
        if is_doc_file(path):
            continue
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            text = f.read()
        for m in LINK_PATTERN.finditer(text):
            base, _, _ = split_link_target(m.group(1))
            if base in real_targets or base in alias_index:
                continue
            broken.append((path, base))
    return broken


def looks_compiled(path):
    with open(path, encoding="utf-8") as f:
        head = f.read(2000)
    return (
        head.startswith("---\ntype: source")
        and re.search(r"^sourceId:", head, re.MULTILINE)
        and "## Summary" in head
    )


def relocate_pending_articles(broken_targets, dry_run):
    moved = []
    seen = set()
    for _, target in broken_targets:
        if target in seen:
            continue
        matches = glob.glob(os.path.join(INPUTS_ARTICLES_DIR, "*", f"{target}.md"))
        if not matches:
            continue
        src = matches[0]
        if not looks_compiled(src):
            continue
        month = os.path.basename(os.path.dirname(src))
        dest_dir = os.path.join(ENTITIES_DIR, "article", month)
        dest = os.path.join(dest_dir, f"{target}.md")
        seen.add(target)
        if not dry_run:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(src, dest)
        moved.append((target, month))
    return moved


def append_relocation_log(moved):
    if not moved:
        return
    log_path = os.path.join(ENTITIES_DIR, "article", "log.md")
    lines = []
    n = len(moved)
    for i, (name, month) in enumerate(moved):
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        suffix = " | reasoning: last of this batch; surfaced as a broken wikilink target during routine lint." if i == n - 1 else " | reasoning: surfaced as a broken wikilink target during routine lint; see the entry above for detail." if i > 0 else (
            " | reasoning: this entry and the "
            + str(n - 1)
            + " below it were auto-relocated by `scripts/fix_links.py` -- each was independently verified to "
            "already carry a full compiled template (frontmatter starting `type: source`, a `sourceId` field, "
            "and a `## Summary` section) before being moved, so no new compilation was performed, only the "
            "missing relocation and this log entry."
        )
        lines.append(
            f"- {ts} (auto-relocated) | source: link-lint auto-fix (`scripts/fix_links.py`) | "
            f"entity: [[{name}]] | action: relocated from `Inputs/articles/{month}/` to "
            f"`entities/article/{month}/`, same filename (content already compiled){suffix}"
        )
    with open(log_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def regenerate_article_catalog():
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "generate_catalog.py"), "article"],
        check=True,
        cwd=ROOT,
    )


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    skip_relocate = "--skip-relocate" in args
    skip_alias = "--skip-alias" in args
    skip_yaml = "--skip-yaml" in args
    skip_coverage_labels = "--skip-coverage-labels" in args
    skip_entity_body_links = "--skip-entity-body-links" in args
    skip_normalize = "--skip-normalize" in args
    do_link_entities = "--link-entities" in args
    no_run_log = "--no-run-log" in args
    domain = None
    if "--domain" in args:
        domain = args[args.index("--domain") + 1]

    scanned_notes_for_run = [p for p in find_all_notes(domain_filter=domain) if not is_doc_file(p)]
    run = None
    if not no_run_log:
        run = RunLogger(
            "lint",
            metadata={
                "script": "fix_links.py",
                "dryRun": dry_run,
                "domain": domain,
                "skipRelocate": skip_relocate,
                "skipAlias": skip_alias,
                "skipYaml": skip_yaml,
                "skipCoverageLabels": skip_coverage_labels,
                "skipEntityBodyLinks": skip_entity_body_links,
                "skipNormalize": skip_normalize,
                "linkEntities": do_link_entities,
            },
        )

    label = "DRY RUN -- no files written" if dry_run else "APPLIED"
    print(f"=== {label} ===\n")

    # --- Pass 0: normalize structural corruption (nested / unbalanced links).
    #     Runs first so every later pass sees balanced, non-nested brackets.
    normalized = []
    if not skip_normalize:
        for path in find_all_notes(domain_filter=domain):
            if is_doc_file(path):
                continue
            full = os.path.join(ROOT, path)
            with open(full, encoding="utf-8") as f:
                text = f.read()
            new_text, changed = normalize_links(text)
            if changed:
                normalized.append(path)
                if not dry_run:
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(new_text)

    yaml_fixed = []
    yaml_unfixed = []
    alias_fixed = []
    all_notes_for_yaml_alias = find_all_notes(domain_filter=domain)

    # --- Pass 1 & 3 happen together per file: YAML fix first (frontmatter),
    #     then alias-only rewrite (body), since a YAML fix can restore a
    #     dropped `aliases:` list that the alias pass then depends on.
    for path in all_notes_for_yaml_alias:
        if is_doc_file(path):
            continue
        full = os.path.join(ROOT, path)
        with open(full, encoding="utf-8") as f:
            text = f.read()

        fm, body, ok = split_frontmatter(text)
        if ok and not try_parse(fm):
            if skip_yaml:
                yaml_unfixed.append((path, "skipped (--skip-yaml)"))
            else:
                fixed_fm, applied = try_fix_yaml_error(fm)
                if fixed_fm is not None:
                    text = "---" + fixed_fm + "---" + body
                    yaml_fixed.append((path, applied))
                    if not dry_run:
                        with open(full, "w", encoding="utf-8") as f:
                            f.write(text)
                else:
                    try:
                        yaml.safe_load(fm)
                    except yaml.YAMLError as e:
                        yaml_unfixed.append((path, str(e).splitlines()[0]))

    # Rebuild context now that YAML fixes may have restored some aliases.
    real_stems, alias_index, remaining_yaml_errors = build_context(find_all_notes())

    if not skip_alias:
        for path in all_notes_for_yaml_alias:
            if is_doc_file(path):
                continue
            full = os.path.join(ROOT, path)
            with open(full, encoding="utf-8") as f:
                text = f.read()
            report = []
            new_text, changed = rewrite_alias_only_links(text, real_stems, alias_index, report, path)
            if changed:
                alias_fixed.extend(report)
                if not dry_run:
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(new_text)

    # --- Pass 2: relocate compiled-but-stranded articles, to a fixed point.
    moved_total = []
    if not skip_relocate:
        while True:
            all_notes = find_all_notes()
            real_stems, alias_index, _ = build_context(all_notes)
            broken = find_broken_targets(all_notes, real_stems, alias_index)
            moved = relocate_pending_articles(broken, dry_run)
            if not moved:
                break
            moved_total.extend(moved)
            if dry_run:
                break  # can't actually move in dry-run, so don't loop forever

        if moved_total and not dry_run:
            regenerate_article_catalog()
            append_relocation_log(moved_total)

    # --- Pass 4: regenerate malformed Coverage labels. This deliberately
    # runs after relocation, so a cited article that was just moved from Inputs
    # can immediately supply the replacement label from its own Summary.
    coverage_labels_fixed = 0
    coverage_label_files = 0
    coverage_label_skipped = []
    if not skip_coverage_labels:
        domains = (domain,) if domain else None
        coverage_labels_fixed, coverage_label_files, coverage_label_skipped = regenerate(
            dry_run=dry_run,
            log=True,
            domains=domains,
        )

    # --- Pass 5: connect otherwise-orphaned entity notes using explicit,
    # unambiguous peer names in their body prose.
    orphan_entity_changes = []
    if not skip_entity_body_links:
        domains = (domain,) if domain else None
        orphan_entity_changes = link_orphans(dry_run=dry_run, log=True, domains=domains)

    # --- Pass 6 (opt-in): backfill first-mention prose links in article notes.
    #     Runs last, and only after Pass 0 has guaranteed balanced brackets.
    entity_links = []   # (path, [(surface, slug), ...])
    if do_link_entities:
        slug_names, name_to_slug = build_entity_indexes()
        for path in find_all_notes(domain_filter=domain):
            if not path.startswith(ARTICLE_PREFIX) or is_doc_file(path):
                continue
            full = os.path.join(ROOT, path)
            with open(full, encoding="utf-8") as f:
                text = f.read()
            new_text, added = link_entities_in_article(text, slug_names, name_to_slug)
            if added:
                entity_links.append((path, added))
                if not dry_run:
                    with open(full, "w", encoding="utf-8") as f:
                        f.write(new_text)

    # --- Final report: re-scan for whatever is left. In --dry-run, nothing
    # was actually written, so filter out anything already shown above as
    # "would be fixed" -- otherwise every dry-run finding would be double
    # counted (once as fixable, once as still-broken-on-disk).
    all_notes = find_all_notes()
    real_stems, alias_index, final_yaml_errors = build_context(all_notes)
    still_broken = find_broken_targets(all_notes, real_stems, alias_index)

    if dry_run:
        would_fix_paths = {p for p, _ in yaml_fixed}
        final_yaml_errors = {p: e for p, e in final_yaml_errors.items() if p not in would_fix_paths}
        would_relocate_names = {name for name, _ in moved_total}
        still_broken = [(p, t) for p, t in still_broken if t not in would_relocate_names]

    print(f"Structural links normalized (nested/unbalanced repaired): {len(normalized)}")
    for path in normalized[:20]:
        print(f"  {path}")
    if len(normalized) > 20:
        print(f"  ... and {len(normalized) - 20} more")
    print()
    print(f"YAML fixed: {len(yaml_fixed)}")
    for path, applied in yaml_fixed:
        print(f"  {path}: {', '.join(applied)}")
    print(f"\nAlias-only links rewritten to piped form: {len(alias_fixed)}")
    for path, old, new in alias_fixed[:20]:
        print(f"  {path}: [[{old}]] -> [[{new}]]")
    if len(alias_fixed) > 20:
        print(f"  ... and {len(alias_fixed) - 20} more")
    print(f"\nArticles relocated from Inputs/ to entities/article/: {len(moved_total)}")
    for name, month in moved_total:
        print(f"  {month}/{name}.md")
    if dry_run and not skip_relocate:
        print("  (dry-run only relocates one round -- a real run loops to a fixed point, so more may surface)")
    print(f"\nMalformed Coverage labels regenerated: {coverage_labels_fixed} across {coverage_label_files} entity notes")
    if coverage_label_skipped:
        print(f"  {len(coverage_label_skipped)} skipped: cited article lacked a usable Summary")
    orphan_entity_link_count = sum(len(added) for _domain, _slug, added in orphan_entity_changes)
    print(f"\nOrphan entity-body links added: {orphan_entity_link_count} across {len(orphan_entity_changes)} entity notes")
    for changed_domain, slug, added in orphan_entity_changes[:20]:
        print(f"  {changed_domain}/{slug}: {len(added)}")
    if len(orphan_entity_changes) > 20:
        print(f"  ... and {len(orphan_entity_changes) - 20} more")

    if do_link_entities:
        total_added = sum(len(a) for _, a in entity_links)
        print(f"\nProse entity links backfilled: {total_added} across {len(entity_links)} articles")
        for path, added in entity_links[:15]:
            print(f"  {path}: " + ", ".join(f"{s}->{sl}" for s, sl in added))
        if len(entity_links) > 15:
            print(f"  ... and {len(entity_links) - 15} more articles")

    print(f"\n--- Remaining (needs manual attention) ---")
    print(f"YAML errors: {len(final_yaml_errors)}")
    for path, err in final_yaml_errors.items():
        print(f"  {path}: {err}")
    print(f"Broken links (no match in entities/ or Inputs/articles/): {len(still_broken)}")
    for path, target in still_broken:
        print(f"  {path}: [[{target}]] -> create via entity_cascade_procedure.md, or fix the citing link")

    remaining = len(final_yaml_errors) + len(still_broken)
    if remaining == 0:
        print("\nCLEAN: nothing left requiring manual attention.")

    if run:
        article_count = len([p for p in scanned_notes_for_run if p.startswith(ARTICLE_PREFIX)])
        total_added = sum(len(a) for _, a in entity_links)
        changed_files = len(
            set(normalized)
            | {p for p, _applied in yaml_fixed}
            | {p for p, _old, _new in alias_fixed}
            | {f"entities/article/{month}/{name}.md" for name, month in moved_total}
            | {f"entities/{changed_domain}/{slug}.md" for changed_domain, slug, _added in orphan_entity_changes}
            | {p for p, _added in entity_links}
        )
        run.status = "failed" if remaining else "ok"
        run.set_article_metrics(
            inputCount=article_count,
            processedCount=article_count,
            updatedCount=len(moved_total) + total_added,
            skippedCount=len(coverage_label_skipped),
            failedCount=remaining,
        )
        run.set_file_metrics(
            scannedCount=len(scanned_notes_for_run),
            changedCount=changed_files,
            failedCount=remaining,
        )
        run.add_output("structuralLinksNormalized", len(normalized))
        run.add_output("yamlFixed", len(yaml_fixed))
        run.add_output("aliasOnlyLinksRewritten", len(alias_fixed))
        run.add_output("articlesRelocated", len(moved_total))
        run.add_output("coverageLabelsRegenerated", coverage_labels_fixed)
        run.add_output("coverageLabelFilesChanged", coverage_label_files)
        run.add_output("orphanEntityBodyLinksAdded", orphan_entity_link_count)
        run.add_output("proseEntityLinksAdded", total_added)
        run.add_output("remainingYamlErrors", len(final_yaml_errors))
        run.add_output("remainingBrokenLinks", len(still_broken))
        for path, err in final_yaml_errors.items():
            run.add_error(err, path=path, kind="yaml")
        for path, target in still_broken:
            run.add_error("broken link remains", path=path, target=target, kind="wikilink")
        if remaining:
            run.status = "failed"
        run.finish()
    sys.exit(1 if remaining else 0)


if __name__ == "__main__":
    main()
