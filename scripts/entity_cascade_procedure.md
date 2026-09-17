---
type: procedure
name: entity-cascade
status: active
last_updated: 2026-08-18
---

# Entity Cascade Procedure

Applies §6's cascade step of Two-Step Ingest: given a compiled article note, create/update every entity note its wikilinks point to, and keep the affected domain's `catalog.md` in sync. Run this against any article note (e.g. `entities/article/2025-11/816663-...md`) or any other note carrying wikilinks.

**Prerequisite — this procedure starts *after* compiling, not before.** A raw item lands in
`Inputs/articles/YYYY-MM/<sourceId>-<slug>.md` (raw feed schema, no cascaded links yet). Compiling
it — populating the frontmatter/body template — and **moving it into `entities/article/YYYY-MM/`
under the same filename** (never left duplicated in `Inputs/`) is the first half of Two-Step Ingest
and must be done before Step 1 below starts. Every example path in this document (e.g.
`entities/article/2025-11/816663-...md`) assumes that move has already happened.

Compilation must also create or preserve the article body's canonical
`## Database Projection` JSON section. An ordinary new input receives a
complete provisional projection with an unallocated UAT identity; imported
legacy evidence preserves its exact allocated projection.

Before compilation begins, the exact selected batch must pass the shared cascade-ready input
contract used by `enrich_radar_inputs.py --check-complete`. `ingest_cascade.py` enforces this again
as one batch-wide, pre-mutation gate. If any selected input is missing an approved classification,
required provenance/count field, publishing outlet, or saved source body—or contains a value outside
the registered enums—stop the whole batch and return it to enrichment or a documented hold. Never
use cascade defaults to replace an unresolved enrichment decision. See
[[enforce-pre-cascade-enrichment-gate]].

## Step 1 — Extract (Analysis only, no writes)

1. Open the article note and extract every `[[wikilink]]` in the body
   (`grep -o '\[\[[^]]*\]\]'` or equivalent).
2. For each linked entity name, classify which domain it belongs to (e.g. `Studio Sector`/`BMTC` →
   `entities/organisations/`, `Pulau Tekong`/`Pasir Ris` → `entities/country/` or a place domain,
   `8Days` → `entities/outlet/`). Use judgment based on what kind of thing the name refers to —
   person, place, organisation, outlet, topic.
   - **Non-cascade domains — `appointments`.** A linked appointment (a role-acronym office like
     `[[cdf]]`, `[[ps-animation]]`, `[[chief-of-navy]]`) belongs to `entities/appointments/`, which is
     **not** cascade-populated. You still fix the link up to piped form (Step 3 item 11), but you do
     **not** create the note, add `## Coverage`, or bump any count for it here — it is maintained
     separately from official Studio Sector pages. If the appointment note doesn't exist yet, that's not a
     cascade gap to fill inline; leave it and flag it. See [[add-appointments-domain]].
3. Check whether a note for that entity **already exists** in the target domain folder (search by
   filename and any `aliases:` in existing notes' frontmatter) — never create a duplicate. If a match
   is found, skip to Step 3 (link fix-up only).
   - **Controlled-vocabulary exception — `topic`.** Do not create a missing Topic Entity inline.
     Hold the affected article and follow `scripts/add_topic.md`; only resume the cascade after the
     topic exists in both `topics/canonical-topics.yaml` and `entities/topic/`.

## Step 2 — Compile (one entity at a time)

For each entity that doesn't yet exist:

4. Confirm the target domain folder exists under `entities/`. If it doesn't exist yet, that's a
   prerequisite gap — stop and scaffold the domain first (an `index.md` following the same pattern as
   the existing domain index files), don't invent an ad hoc note location.
5. Read that domain's `index.md` — pull its frozen YAML registry (the field table) and its
   `## Template` section.
6. Copy the template verbatim as the starting point for the new note.
7. Populate the YAML frontmatter using only the registry's field set for that domain — no invented
   fields, no relationship lists in YAML.
8. Write the Markdown body per the template's required sections, and populate **both** link
   directions — this note is never an isolated island:
   - **Every cross-reference link — to the article, to peer entities, anywhere — must be the
     piped form `[[<real-filename>|<Display Name>]]`, never bare `[[Display Name]]`.** e.g.
     `[[816663-are-nsfs-really-banned-from-dining-at-mcdonalds-in-white-sands|8Days coverage]]`,
     `[[bmtc|BMTC]]`, `[[southeast-asia|Southeast Asia]]`. This is **mandatory, not a style
     preference** — see "Why piped links, always" below.
   - **Backlink to the source (required).** Populate the template's `## Coverage` section with a
     piped link back to the citing article, e.g. `[[<sourceId>-<slug>|<short label>]]`. Every domain
     template (`outlet`, `organisations`, `people`, `topic`, `country`, `place`) reserves the
     `## Coverage` section for exactly this — skipping it leaves the note untraceable to why it
     exists and breaks the citability requirement in §6's Query operation. **Exception — the
     `appointments` domain is not cascade-populated:** appointment notes have no `## Coverage` and no
     `mentionCount`; their holder facts come from official Studio Sector pages, and holders are recorded as
     dated `[[wikilinks]]` under a `## Holders` section. An article that wikilinks an appointment
     (e.g. `[[cdf]]`) just resolves to the existing note — never add a Coverage line, create the note
     inline, or run it through `patch_coverage.py`.
   - **Outgoing links to peer entities.** Link this note to whatever it's naturally related to —
     e.g. the `Studio Sector` note should link to `[[bmtc|BMTC]]` and `[[southeast-asia|Southeast Asia]]`;
     a place note should link to its containing area the same way.
   - **YAML flow-list values must be quoted if they start with `#`.** `tags: [#animation-industry]` is **invalid
     YAML** — inside `[...]`, an unquoted `#` is read as a comment, so the parser never finds the
     closing `]` and the *entire frontmatter block fails to parse* (not just `tags` — `aliases`,
     every field, gone). Always write `tags: ['#animation-industry']` / `tags: ['#source', '#animation-industry']`. This is the
     single most expensive bug found while building this procedure.
   - **No live web enrichment.** Populate the body only from what the citing article(s) actually
     say — never from a live internet search or model background knowledge. If genuine external
     enrichment is wanted, land the source in `Inbox/Links/` first (URL + fetch timestamp) and cite
     it explicitly (§1 traceability; §7 "organized lie" failure mode). This is especially strict for
     `#animation-industry`-flagged entities (§10).
9. Name and save the file per that domain's naming convention documented in its `index.md`. Still
   populate `aliases:` with the display name (useful for Quick Switcher and manual typing) — just
   don't *rely* on it for the links this procedure itself writes; those are always piped (see above).

## Step 3 — Cascade & Reconcile

10. **Existing-entity branch (from Step 1, item 3).** For every linked entity that already had a note,
    don't skip it silently — cascade the new reference into it. **(Skip `appointments` entirely: an
    appointment note has no `## Coverage`/`mentionCount` and must never be fed to `patch_coverage.py`,
    which rejects the domain with a `ValueError` by design. A link to an appointment needs only the
    piped-form fix-up in item 11.)**
    - Add a piped backlink (see Step 2 item 8) for *this* article to the entity's `## Coverage`
      section if it isn't already there (check first — never duplicate an existing backlink).
    - Increment `mentionCount` by 1 — **except for `outlet`'s `articleCount`**, which is a
      pre-computed grand total over the *raw* corpus at ingestion (see `entities/outlet/index.md`
      item 1) and must **not** be incremented when cascading an article that already existed in that
      raw corpus (true for essentially every article this procedure touches). Only bump `articleCount`
      for a genuinely new raw article arriving after the aggregate was last computed. Getting this
      wrong silently double-counts — see [[fix-articlecount-double-counting]].
    - Merge in any new outgoing wikilinks this article's context reveals (e.g. a newly-mentioned
      affiliation), without removing existing ones.
    - **`scripts/patch_coverage.py` is the single implementation of Coverage insertion and count
      bumping. No other script may reimplement it — import `apply_update` instead.** This rule was
      added after `ingest_cascade.py` was found carrying its own inline copy that appended backlinks
      to end-of-file rather than into the `## Coverage` block, stranding them under whatever section
      came last (695 notes, 58,341 links, repaired by `scripts/repair_stranded_coverage.py`). Two
      copies of this logic also means two copies of the [[fix-articlecount-double-counting]] carve-out.
    - **`scripts/patch_coverage.py`** does the mechanical part of this bullet (and the `mentionCount`/
      `articleCount` bump above) for a whole batch in one pass: feed it a JSON list of `{domain, id,
      article, label}` rows and it appends the missing `## Coverage` lines and recomputes the count
      field, skipping any `(entity, article)` pair already present — safe to re-run. It does **not**
      decide which entities are new, does **not** write `## Summary` prose, and does **not** touch
      `## Related Entities`; those stay manual. Built after this session's `mentionCount` arithmetic
      mistakes (e.g. the Ng Eng Hen overcount) made clear the bookkeeping, not the judgment, was where
      batches were going wrong. `--dry-run` previews without writing.
11. Fix up the *source* article's wikilinks so every one is the piped `[[<real-filename>|<Display
    Name>]]` form (e.g. `[[8days|8Days]]`, not bare `[[8Days]]` and not folder-qualified
    `[[Outlet/8Days]]`). Do this **regardless of whether an `aliases:` entry could also resolve it** —
    don't rely on alias resolution as the fix; it proved unreliable in practice (see below). Populating
    `aliases:` is still good practice for humans searching the vault, just not a substitute for the
    piped link.
12. Regenerate the affected domain's `catalog.md` — run
    `python3 scripts/generate_catalog.py <domain>` for every domain that got a new or updated note.
13. **Append an entry to the domain's `log.md` for every single entity created or updated in that
    domain — never batch multiple entities from a batch cascade into one line, and never skip this
    even for a one-field change (e.g. a `mentionCount` bump).** Each entry is mandatory to carry:
    - **A full timestamp (date *and* time)**, captured at the moment the entry is written — e.g.
      `` `date "+%Y-%m-%dT%H:%M:%S"` ``. A date alone (`2026-07-06`) is not sufficient; multiple
      entries on the same day must be orderable.
    - **A `[[wikilink]]` to the entity file itself** (not a backticked filename) — e.g. `[[studio-sector]]`,
      not `` `studio-sector.md` ``. The link must point at the file's *current* real name; if you're
      recording a rename in the same entry, link the new name and mention the old one in prose.
    - The triggering source (the article, as a piped link), the action taken, and the reasoning —
      as before.
    - Template: `- <timestamp> | source: [[<article>|<label>]] | entity: [[<entity>]] | action:
      <created|updated> — <what changed> | reasoning: <why>`
    - If one cascade run touches five entities, that's five `log.md` lines (across whichever
      domains' logs they belong to), not one summarizing line — the wikilink only does its job if
      it names exactly one file.
14. **Quality and lint gates — run `python3 scripts/article_quality.py --check --path
    <compiled-article.md> --no-run-log`, then `python3 scripts/check_links.py`, before considering the
    cascade done; treat either non-zero exit as "not finished".** The first command enforces the
    frozen article registry, native-ID/filename agreement, enums, required values, publication-month
    placement, and compiled sections. The second is the link postcondition: a linking pass must never
    *emit* the corruption it checks for. It reports, by severity —

    **Hard failures (exit 1 — must be fixed before the cascade is done):**
    - **BROKEN** — target matches no file and no alias (a genuine missing entity — create it, Step 2).
    - **NESTED (target-embedded)** — an inner link embedded in another link's *slug*, e.g.
      `[[1000895-[[japan|Japan]]-[[canada|Canada]]-sign|...]]`; the target no longer resolves. Emitted
      by naive auto-linking that rewrites an entity name sitting inside an existing link.
    - **YAML ERRORS** — frontmatter that fails to parse (what silently drops `aliases` — Step 2 item 8).

    **Advisories (exit 0 — surfaced, not blocking):**
    - **MALFORMED COVERAGE LABELS** — a link embedded in a *truncated* `## Coverage` label (the outer
      link's target still resolves; the label needs regenerating, not bracket surgery).
    - **ALIAS-ONLY** — resolves today but via `aliases:` only; rewrite to piped form.
    - **UNLINKED ENTITY** — a known entity named in an article's prose but never linked in that article
      (a missed first-mention link).

    **Auto-repair — `python3 scripts/fix_links.py`** fixes the mechanical classes so the gate can go
    green: it normalizes target-embedded NESTED links and isolated single-`]` closes (Pass 0, always
    on), rewrites ALIAS-ONLY links to piped form, relocates compiled-but-stranded articles, repairs
    the two known YAML corruptions, and regenerates malformed Coverage labels from the cited article's
    own `## Summary` while retaining the valid outer target. It also links an otherwise-orphaned
    entity note to explicitly named peers in its body, using only unambiguous multiword names and
    uppercase acronyms outside YAML, Coverage, Source, AI Context, and template comments. Add
    `--link-entities` to also backfill first-mention prose links for the UNLINKED ENTITY class.
    `--dry-run` previews. It never guesses at genuine BROKEN links — those are left for a human to
    create or resolve.
    A clean `check_links.py` prints `CLEAN` and exits `0`; a hard failure means this cascade isn't done.
15. **Keep the cascade local.** `ingest_cascade.py` must finish compilation, entity updates,
    catalogs, focused validation, status bookkeeping, and its receipt without invoking projection
    or database tools. For a frozen multi-month batch, prepare and verify exactly one final UAT
    delta only after every selected month and the batch-wide quality/link gates pass:
    - prepare a hashed bundle with `project_wiki_to_uat.py prepare-current`;
    - verify it, allocate stable UAT IDs into local Markdown projections, and verify it again;
    - compare every existing parent/child multiset against live UAT;
    - report inserts, updates, deletes, elapsed time, and average seconds per article.

    This stage never loads the database. A UAT transaction requires a separate, attributed approval
    bound to the verified bundle ID. Any proposed delete, unexpected update, identity mismatch, or
    production target is a breakout. The historical `stage_mysql_feeds.py` and
    `stage_enriched_radar_inputs.py` paths must not be used for new loads.

## Step 4 — Repeat

16. Do this for every distinct wikilink extracted in Step 1 before moving to the next article.

## Batch processing (multiple articles at once)

When cascading a batch of N articles rather than one, don't read and draft them one at a time —
fan the drafting step out to parallel subagents (the `Agent` tool, one call per article, all issued
in a single message so they run concurrently), then reconcile serially:

1. **Parallel (subagents).** Each agent gets one article and a self-contained brief: the raw
   article's path, the domain's `index.md` template, this procedure's conventions (piped links only,
   quote any `#`-tag, no live web enrichment), and instructions to cross-check every entity it would
   link against that domain's `catalog.md` to mark it new vs. existing. It must **not** write or edit
   any vault files — it returns (a) the full compiled article text and (b) a JSON list of
   `{domain, id, is_new, label, one-line summary if new}`.
2. **Serial (you).** Skim all N drafts together before writing anything — this is the one step a
   single agent can't do for itself: catching two articles in the same batch that turn out to be the
   same underlying event (as happened with article `1012730`), or two agents proposing different IDs
   for what's really one entity (`ng-eng-hen` vs. `dr-ng`). Then write the article files, create
   agreed-new entities by hand (Step 2, unchanged), merge every agent's existing-entity rows into one
   JSON file and run `scripts/patch_coverage.py` once (it groups by `(domain, id)` on its own, so
   several agents touching e.g. `southeast-asia` collapses into one correct update), then proceed to Step 3
   items 12–13 (catalog regen, logging) as normal.

No special trigger phrase is needed to invoke this — `Agent` calls don't require the opt-in
`Workflow` does. Default to this fan-out for any batch above a handful of articles; for a single
article, just do Steps 1–4 directly.

## Why piped links, always

Found the hard way on 2026-07-06 while cascading article `816663`, via hours of live testing directly
in Obsidian (Quick Switcher, Outgoing Links pane, in-body clicks, full vault reloads): bare `[[Display
Name]]` links depend on Obsidian's `aliases:` frontmatter resolution, and that resolution is fragile
in two independent ways —

1. **It silently breaks if the target's frontmatter fails to parse at all** — and an unquoted `#` in
   a YAML flow list (`tags: [#animation-industry]`) does exactly that (see Step 2 item 8). The note looks fine, the
   `aliases:` field looks fine, but the parser never produces a valid document, so `aliases` is
   never registered.
2. **Even with valid YAML, in-body link clicks and the Outgoing Links pane can show stale
   resolution** independent of what Quick Switcher reports for the same alias at the same moment —
   closing and reopening the tab, or even a full vault reload via vault-switching, did not
   reliably clear it in testing.

Every failure mode above disappears if the link names the real file directly: `[[bmtc|BMTC]]`
cannot fail to resolve regardless of `aliases:`, `tags:`, or cache state — `bmtc` **is** the
filename (entity domains dropped the old doubled `<id>-<id>.md` convention on 2026-07-06 for
exactly this reason — see [[drop-doubled-slug-filenames]]). Piped links cost a few extra characters
and are otherwise identical in rendered output
(Obsidian displays only the text after `|`). There is no situation where the bare form is worth the
risk. Do not revert this without re-confirming (live, in Obsidian, not just via a YAML linter) that
bare alias resolution is actually reliable — it wasn't, twice, in the same session.
