---
type: operating-plan
name: tag-entity-source-of-truth
status: active
last_updated: 2026-08-18
---

# Tag Entity Source-of-Truth Plan

## Objectives

1. Make `entities/tag/` the authoritative vocabulary for issue-radar tags.
2. Govern tag approval and retirement through an auditable status lifecycle.
3. Replace the dated CSV as the enrichment vocabulary source.
4. Preserve compiled Markdown as the source of article-to-tag assignments.
5. Synchronize those assignments through the existing `UAT_article_tags` table without adding a
   new database table or writing to production.
6. Require a reproducible, read-only Issue Radar dry run as the final acceptance test.

## Status model

The only permitted statuses are:

- `awaiting_approval` — proposed and retained for review; cannot be assigned to articles.
- `active` — approved for enrichment, article assignment, UAT projection, and radar evaluation.
- `inactive` — temporarily unavailable for new assignments and excluded from radar on or after its
  effective date; earlier evidence is retained.
- `deprecated` — permanently retired; requires a replacement tag or an explicit no-replacement
  reason.
- `rejected` — declined and retained to suppress duplicate proposals; cannot be assigned.

Allowed transitions are `awaiting_approval` to `active` or `rejected`; `active` to `inactive` or
`deprecated`; `inactive` to `active` or `deprecated`; and `rejected` to `awaiting_approval` when new
evidence exists. `deprecated` is terminal. Every transition requires an effective timestamp, actor,
reason, and append-only log entry. Activation requires attributed human approval.

## Entry conditions

Start only when all of the following are true:

1. `AGENTS.md` and `README.md` have been read.
2. The pre-change automated test baseline is recorded.
3. The current CSV, compiled Markdown projections, and UAT tag assignments have been inventoried.
4. No concurrent ingest, projection load, or radar filing run is active.
5. Existing worktree changes are preserved and overlapping files are changed surgically.
6. Production remains read-only.

## Eight-step implementation sequence

### 1. Approve governance

Create a Decision note authorizing the Tag domain, frozen registry, statuses, approval rules,
historical behavior, CSV retirement, and UAT validation rules. Append the decision-domain log.

### 2. Create the Tag domain

Create `entities/tag/` with `index.md`, `_template.md`, generated `catalog.md`, and append-only
`log.md`. Register the domain in the vault documentation, manifest, link handling, and catalog
workflow.

### 3. Define and validate the schema

Each Tag note uses a flat `<tagId>.md` filename and the fields `tagId`, `displayName`, `aliases`,
`status`, `statusEffectiveAt`, `approvedAt`, `approvedBy`, and `articleCount`. Its body contains
`Definition`, `Replacement`, `Status History`, `Coverage`, and `Notes`. Add deterministic validation
for schema, IDs, aliases, statuses, transitions, links, coverage counts, and generated catalog.

### 4. Migrate existing tags

Build the migration universe from the historical production-tag CSV plus every tag found in
compiled `## Database Projection` sections. Normalize exact duplicates without inventing semantic
merges. CSV-listed or currently assigned canonical tags begin `active`; unresolved normalized-name
or alias collisions are breakout conditions. Create Tag notes, article backlinks, counts, catalog,
log, and a migration receipt. Preserve the CSV as historical evidence only.

### 5. Update enrichment

Replace runtime CSV lookup with the active Tag entity registry. Only active canonical display names
may be selected or applied. Evidence-backed unmatched concepts are reported as proposals; they do
not enter article frontmatter, projections, or UAT before approval.

### 6. Implement tag review and article links

Create proposed entities as `awaiting_approval` only after evidence and duplicate checks. Require
attributed approval before activation. Compile article `## Issue Tags` as canonical piped links
`[[tag/<tagId>|<displayName>]]`, while preserving canonical display strings in Database Projection
`tags`. Existing plain issue-tag bullets are migrated deterministically.

### 7. Synchronize and verify UAT assignments

Continue using `project_wiki_to_uat.py` as the sole writer. Validate every projected tag against the
Tag entity registry before bundle preparation or load. Compare compiled projection tag multisets
with `UAT_article_tags`; require zero unexplained differences and an idempotent second diff. Do not
create a new UAT table. Never write to production.

### 8. Run the final Issue Radar dry run

After all other gates pass, freeze the evaluation date and manifest, run the radar against UAT at
WATCH level, save structured and readable output, repeat it, and require identical hashes.
Independently reproduce every WARM and HOT score and confirm every candidate resolves to a Tag
entity active at the evaluation date. This is a dry run: do not file or update Issue notes, catalogs,
or logs, and do not write to UAT or production.

## Breakout conditions

Stop, preserve evidence, and report `FAIL` or `CONDITIONAL PASS` when any of these occurs:

1. A canonical-name or alias collision cannot be resolved deterministically.
2. A compiled or UAT article tag lacks a valid Tag entity.
3. A non-active tag is newly assigned or projected.
4. A transition lacks valid attribution, timestamp, or reason.
5. A deprecated tag lacks a replacement or explicit no-replacement reason.
6. Markdown-to-UAT tag parity is not exact.
7. Schema, catalog, log, or wikilink validation fails.
8. A required automated test fails.
9. The final radar outputs are not reproducible or a candidate lacks an active Tag entity.
10. Any Issue file changes during the dry run or any production write is attempted.

## Required tests

### Tag registry and lifecycle

- Required and unknown fields, field types, filename/ID agreement, and all five statuses.
- Every allowed and forbidden status transition.
- Approval fields for active tags and retirement evidence for deprecated tags.
- Case-insensitive uniqueness of IDs, names, and aliases.
- Status-history agreement with current frontmatter.
- Coverage count, catalog consistency, append-only log, and wikilink integrity.

### Migration and enrichment

- Complete CSV/projection inventory; deterministic normalization; collision breakout.
- No lost or duplicate article assignments; idempotent migration rerun.
- Only active entities are shortlisted, selected, reconciled, or applied.
- Non-active and unknown values are rejected; unmatched concepts remain proposals.
- No runtime dependency on `production-tags.csv` remains.

### Article projection and UAT

- `## Issue Tags` links resolve to canonical Tag entities and exactly match projection strings.
- Unknown or non-active tags stop compile, cascade, projection preparation, and load.
- Tag backlinks and counts update idempotently.
- Projection/UAT tag multisets match exactly; repeated synchronization yields zero delta.
- Production targets remain prohibited.

### Final Issue Radar dry run

- All preceding gates pass before the run.
- Two WATCH-level UAT runs produce identical structured and readable outputs.
- Every candidate tag is active at the frozen evaluation date.
- Every WARM and HOT result reproduces independently.
- No Issue files, UAT rows, or production rows change during the dry run.

## Deliverables and acceptance

Deliver the Decision note, Tag domain and notes, validators and workflow changes, migration receipt,
updated tests and documentation, exact Markdown/UAT parity evidence, dry-run artifacts, and a final
result of `PASS`, `CONDITIONAL PASS`, or `FAIL`.

`PASS` requires all tests and validators to pass, every assigned tag to resolve to exactly one
active Tag entity, no runtime CSV dependency, exact UAT parity, an idempotent repeat diff, identical
radar dry-run hashes, independent WARM/HOT verification, no Issue-file changes, and zero production
writes.
