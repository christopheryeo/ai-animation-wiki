---
type: operating-plan
name: tag-universe-optimization
status: completed
last_updated: 2026-08-18
---

# Tag Universe Optimization Plan

## Summary and objectives

Optimize the Issue Radar's working tag set without deleting historical Tag entities or changing
article-to-tag assignments.

Objectives:

1. Retain all 4,951 Tag entities as historical records.
2. Reduce the radar-enabled universe by an initial 20–30%: approximately 990–1,485 tags.
3. Preserve useful tags for enrichment even when they are unsuitable for radar scoring.
4. Protect rare but strategically important early-warning tags.
5. Improve radar precision without missing known Severe or High issues.
6. Keep Markdown as the source of truth; add no database table or operational CSV.
7. Make every eligibility change attributed, effective-dated, reversible, and testable.

## Tag interface changes

Add two fields to every Tag entity:

- `radarStatus`: `enabled`, `shadow`, or `disabled`
- `radarStatusEffectiveAt`: effective timestamp

Add an append-only `## Radar Status History` table containing timestamp, state, actor, reason, and
evidence reference.

Behavior:

- `active + enabled`: assigned by enrichment and scored operationally.
- `active + shadow`: assigned normally, scored only in comparison output, and never surfaced.
- `active + disabled`: assigned normally but excluded from radar scoring.
- Any non-active lifecycle status: not newly assigned or operationally scored, regardless of
  `radarStatus`.
- Existing tags begin `enabled`.
- UAT article-tag assignments remain unchanged.
- Radar JSON records the Tag-registry hash and enabled, shadow, and disabled counts.
- Add an optional shadow-output path to `issue_radar.py`; default operational behavior returns
  enabled tags only.

## Eight-step implementation sequence

### 1. Close the current reconciliation gate

- Reconcile the 124 non-canonical article sections and 440 affected Tag notes.
- Require Tag validation, article quality, link integrity, and current Markdown/UAT parity.
- Do not begin optimization while ingestion or projection is active.

### 2. Approve the schema amendment

- Record a Decision note adding radar eligibility separately from lifecycle status.
- Update the Tag schema, template, validator, documentation, and transition rules.
- Preserve all existing lifecycle statuses and meanings.

### 3. Freeze the baseline

- Record the 4,951-tag registry hash, current UAT bundle, evaluation dates, and benchmark fixtures.
- Run the unchanged radar twice at WATCH level.
- Save candidate counts, tiers, scores, precision, runtime, and Issue-file hashes.

### 4. Generate the optimization ledger

- Create a read-only JSON/Markdown proposal ledger, not an operational CSV.
- Measure article frequency, outlet and country breadth, recency, co-occurrence, historical flags,
  benchmark contribution, and semantic role.
- Prioritize tags appearing in ten or fewer articles, tags producing no useful historical signals,
  generic descriptors, entity-like labels, and strongly redundant tags.
- Protect every tag supporting a known Severe or High benchmark issue.
- Name similarity alone must never justify consolidation or exclusion.

### 5. Review and place candidates in shadow

- Select 990–1,485 candidates through the hybrid evidence review.
- Require an attributed reason for every proposal.
- Move approved candidates from `enabled` to `shadow`; do not move directly to `disabled`.
- Use `deprecated` only for confirmed semantic duplicates with a canonical replacement.

### 6. Run shadow comparisons

- Run enabled and shadow scoring over every maintained benchmark date plus the latest stable UAT
  evaluation date.
- Compare baseline and optimized candidates, scores, tiers, false alerts, missed issues, and
  runtime.
- Independently reproduce all WARM and HOT scores.
- Repeat every run and require identical hashes.

### 7. Approve the optimized working set

- Move shadow tags to `disabled` only when all acceptance tests pass.
- Keep protected or materially useful tags `enabled`.
- Return uncertain tags to `enabled`.
- Append Tag histories, the domain log, and an exact approved-manifest receipt.

### 8. Run final operational acceptance

- Validate the complete Tag registry.
- Run the Issue Radar twice against UAT using enabled tags only.
- Confirm disabled tags remain present in article metadata but produce no operational candidates.
- Confirm no article assignments, UAT rows, production rows, or Issue notes changed.
- Issue a final `PASS`, `CONDITIONAL PASS`, or `FAIL` report.

## Required tests

### Schema and lifecycle

- Required fields, enum values, timestamps, filename/ID agreement, and unknown-field rejection.
- Lifecycle status and radar status operate independently.
- Non-active lifecycle status always overrides radar eligibility.
- Radar Status History agrees with current frontmatter.
- Missing attribution, reason, timestamp, or evidence fails validation.
- Every migrated Tag defaults to `enabled`.

### Selection and governance

- Proposal generation is deterministic and idempotent.
- Protected benchmark tags cannot enter the candidate set.
- Low frequency alone cannot automatically disable a tag.
- Name similarity alone cannot deprecate or merge tags.
- Exact approved-manifest hashing prevents applying a changed proposal.
- Rerunning an approved transition creates no duplicate history or log entry.

### Radar behavior

- Enabled tags appear in operational scoring.
- Shadow tags appear only in shadow comparison output.
- Disabled tags do not appear in operational or shadow results.
- Unknown database tags remain a hard failure.
- All WARM and HOT scores reproduce independently.
- Repeated runs produce byte-identical structured and readable output.
- Registry hash and eligibility counts appear in run metadata.

### Quality and regression

- Surfaced-alert precision remains at least 80%.
- No known Severe or High benchmark issue is missed.
- No maintained benchmark produces a worse expected result.
- WARM/HOT recall is no worse than the baseline.
- False-alert count does not increase.
- Operational candidate volume and runtime are recorded before and after optimization.
- Article assignments and UAT tag multisets remain unchanged.
- Production receives zero writes.
- Full automated test, Tag validation, article-quality, catalog, log, and link checks pass.

## End conditions

`PASS` requires:

1. The radar-enabled universe is reduced by 20–30%.
2. All Tag entities remain preserved and valid.
3. All required tests pass.
4. Benchmark precision is at least 80%.
5. No known Severe or High issue is missed.
6. Baseline and optimized comparisons are reproducible.
7. Markdown/UAT article-tag parity remains exact.
8. No Issue files, UAT rows, or production rows change during testing.
9. The final operational dry run passes twice with identical hashes.

`CONDITIONAL PASS` applies when the implementation works but the reduction target cannot yet be
approved because additional shadow evidence or human review is required.

## Breakout conditions

Stop without disabling tags if:

1. Current Tag reconciliation or Markdown/UAT parity is incomplete.
2. Ingestion, projection, or Issue filing changes the frozen corpus.
3. A benchmark Severe or High issue disappears or drops unexpectedly.
4. Precision falls below 80% or false alerts increase.
5. A candidate lacks an evidence-backed reason or attributed approval.
6. A semantic duplicate lacks a canonical replacement.
7. Schema, history, catalog, log, article, or link validation fails.
8. Baseline or comparison outputs are not reproducible.
9. An unknown UAT tag or invalid eligibility state is found.
10. The proposed reduction requires changing article assignments or database rows.
11. Any production write is attempted.
12. The 20–30% target cannot be reached safely; retain the smaller validated reduction and report
    `CONDITIONAL PASS`.

## Assumptions

- Optimization concerns the radar working set, not deletion of Tag entities.
- Article enrichment may continue using active tags regardless of radar eligibility.
- All eligibility changes require human approval.
- Shadow is the mandatory trial state before disabled.
- Existing Issue Radar weights, thresholds, and tier rules remain unchanged.
