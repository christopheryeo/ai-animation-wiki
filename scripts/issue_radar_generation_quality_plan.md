---
type: operating-plan
name: issue-radar-generation-quality
status: active
last_updated: 2026-07-31
---

# Reusable Issue Radar Generation and Quality Plan

## Purpose

Use this plan after the wiki has been expanded and the new articles have completed the normal
ingest/cascade process. It governs the repeatable end-to-end job of validating the expanded corpus,
running the issue radar, converting tag flags into issue objects, checking quality, filing the
results, and reporting what should be surfaced.

This plan supplements, and does not replace:

- `scripts/issue_radar.py` for deterministic signal computation.
- `scripts/issue_radar_procedure.md` for clustering, ramification, catalysts, posture, and filing.
- `entities/issues/index.md` for the frozen issue-note registry.

## Objectives

1. Detect developing issues whenever the wiki gains new articles.
2. Produce reproducible results from the same corpus and evaluation date.
3. Prevent incomplete or poor-quality article data from distorting the radar.
4. Separate tag-level statistical flags from genuine issue objects.
5. Update existing issues instead of creating duplicates.
6. Measure false alarms, missed issues, reviewer disagreements, and warning lead time.
7. Preserve sufficient evidence to reproduce and audit every run.

## Run parameters

Record these values before every run:

| Parameter | Required value or rule |
|---|---|
| Run ID | Unique timestamp-based identifier |
| Evaluation date | Latest publication date for which ingest and projection are complete |
| Operational source | UAT |
| Comparison source | Production, read-only |
| Minimum captured tier | WATCH |
| Corpus scope | All eligible articles dated on or before the evaluation date |
| Previous run | Most recent completed issue-radar run, if one exists |
| Radar version | Code revision plus the active weights and thresholds |
| Quality threshold | At least 80% precision for surfaced alerts |
| Alert rule | WARM or HOT with MODERATE-or-higher ramification |

Never mix partially processed articles into a run.

## Conditions

### Entry conditions

Start only when all of the following are true:

1. The latest article batch has completed enrichment, compilation, and cascade.
2. The corresponding UAT projection bundle has been prepared and verified.
3. Any required UAT load has explicit attributed approval tied to that exact verified bundle ID.
4. Compiled Markdown and UAT have no unexplained article, tag, or coverage differences.
5. Every included article has a valid publication date and usable issue tags.
6. Outlet, country, category, tone, and event type are valid or explicitly marked for review.
7. No uncertain AI classification has been silently applied.
8. All automated radar and enrichment tests pass.
9. Production access, when used, is SELECT-only.
10. The Tag entity registry validates, every projected/UAT tag resolves, and only tags active at
    the evaluation date are eligible for scoring.

If any entry condition fails, stop and issue a readiness-failure report. Do not run or file the
operational radar results.

### Radar-run conditions

1. Evaluate the complete eligible history, not only the newest batch.
2. Exclude articles published after the evaluation date.
3. Capture every WATCH, WARM, and HOT flag.
4. Preserve the complete unedited radar output and run metadata.
5. Repeat the run and require identical results.
6. Do not change weights, thresholds, stop lists, or individual results during a run.
7. Treat tag flags as candidates, not confirmed issues.
8. Freeze the Tag registry status state used for the evaluation date with the run artifacts.

### Issue-acceptance conditions

A candidate may create or update an issue only when:

1. Its constituent tags share supporting articles or entities.
2. It has been compared with the existing issue catalog.
3. Updating an existing issue has been considered before creating a new one.
4. Its ramification assessment is supported by ingested vault articles.
5. Every catalyst date has an article citation.
6. No assessment claim comes from live-web research or model background knowledge.
7. Its status agrees with its radar score.
8. Its ramification rating agrees with its written assessment.

### Stop conditions

Do not file or surface a result when:

1. Required data is missing without explanation.
2. A score cannot be independently reproduced.
3. UAT and production have unexplained material differences.
4. Clustering is based only on similar tag names.
5. A material assessment or catalyst claim lacks evidence.
6. Schema, wikilink, catalog, or append-only-log validation fails.
7. The quality acceptance criteria below are not met.

## One-time implementation prerequisites

Implemented and regression-tested as of 2026-07-31. Preserve these capabilities in later changes:

1. Add structured JSON output to `scripts/issue_radar.py` while preserving its current readable
   output. Include run metadata, article counts, candidates, six signal components, scores, tiers,
   and plain-language reasons.
2. Expand `tests/test_issue_radar.py` to cover candidate minimums, generic-tag exclusions, all six
   signals, date boundaries, coverage waves, tier boundaries, future-data exclusion, duplicates,
   missing optional values, and stable output ordering.
3. Maintain a labelled benchmark set containing confirmed historical issues, correctly dismissed
   candidates, and difficult borderline cases from different subjects and time periods.
4. Add every later false alert, missed issue, or disputed classification to that benchmark set.
5. Do not change radar weights, thresholds, or schemas while completing these prerequisites.

## Recurring run procedure

### 1. Freeze the run

1. Assign the run ID and evaluation date.
2. Record the latest completed ingest receipt and verified UAT bundle.
3. Create a manifest of every included article ID.
4. Record article, tag, outlet, country, category, tone, and event-type totals.
5. Record the previous completed radar run for comparison.

### 2. Validate the expanded corpus

Run article and projection quality checks across the complete eligible corpus. Report each radar
field as valid, explicitly unavailable, awaiting review, invalid, or changed since the previous
run.

Require:

1. No duplicate article IDs.
2. No invalid dates or future leakage.
3. No invalid category, tone, or event-type values.
4. No unexplained missing tags.
5. No unexplained compiled-Markdown-to-UAT projection differences.

### 3. Run the mechanical radar

1. Run UAT at WATCH level using the frozen evaluation date.
2. Save readable and structured results under the run ID.
3. Repeat the run and compare result hashes.
4. Independently recompute every WARM and HOT result with
   `scripts/verify_issue_radar_output.py`.
5. Recompute a rotating sample of WATCH results.
6. Confirm that every candidate satisfies the eligibility rules.
7. Confirm that every displayed reason matches its underlying signal values.

### 4. Compare with the previous run

Classify every candidate as new, increased tier, decreased tier, unchanged, no longer flagged,
reopened after dismissal, or affected by corrected source data.

Explain material changes using article volume, outlets, countries, institutional categories,
coverage waves, unfacilitated share, and opinionated share. Investigate unexplained large changes
before filing.

### 5. Compare with production

1. Run production read-only with the same evaluation date and radar version.
2. Compare article counts, tag counts, candidates, signal components, scores, and tiers.
3. Classify each difference as an expected UAT addition, expected correction, expected
   production-only history, or unexplained discrepancy.
4. Proceed only when every material difference is explained.
5. Never write to production or use a historical staging script for the comparison.

### 6. Cluster candidate tags

Create the initial disposition ledger and evidence pack with
`scripts/review_issue_radar_run.py`, then complete the judgment fields. The record for every flag
must contain its tag, tier, score, reasons, supporting articles, overlapping entities,
existing-issue match, proposed cluster, disposition, and reasoning.

Cluster using article and entity overlap. Never cluster solely because tag names look similar.

### 7. Assess ramifications

For every proposed issue, answer from vault evidence:

1. Who would be forced to respond if coverage doubled?
2. Does the issue touch an established fault line?
3. Are future catalysts mentioned?
4. Has a senior party taken a difficult-to-reverse position?
5. Is a foreign story attaching to domestic institutions?

Assign:

- `severe` — multiple fault lines or likely minister-level response.
- `high` — one fault line with a likely institutional response.
- `moderate` — contained but recurring.
- `low` — benign or event-shaped.

Acceleration without meaningful ramification is dismissed rather than alerted.

### 8. Perform the second quality review

Conduct an evidence-only second review of every WARM and HOT cluster, every MODERATE-or-higher
assessment, every proposed new issue, every reopened issue, and a rotating sample of WATCH and
dismissed candidates.

The second review may challenge clustering and judgment, but must not change the mechanical radar
output. Record and resolve disagreements before filing.

Use `scripts/local_issue_radar_review.py` when evidence must remain inside the vault. It cannot
surface an alert without a separate, evidence-cited judgment decision. External model review
requires explicit data-sharing approval.

### 9. File the results

For every issue touched:

1. Update an existing note, or create a new note only when necessary.
2. Preserve `firstFlagged` and set `lastScored` to the current run date.
3. Update score, status, and cluster tags.
4. Append the new signal record without rewriting prior signal history.
5. Write cited Assessment, Catalysts, Coverage, and Posture sections.
6. Keep dismissed issues as calibration evidence.
7. Append one issue-domain log entry.
8. Regenerate the Issues catalog.

A pass with no new flags must still review and update active WARM and HOT issues as required by
`scripts/issue_radar_procedure.md`.

### 10. Validate and surface

Validate required fields, unique issue IDs, status and ramification values, status-versus-score
consistency, cluster tags, citations, wikilinks, append-only history, and catalog consistency.

Surface only WARM or HOT issues with MODERATE, HIGH, or SEVERE ramification that passed the second
review. Everything else remains on the quiet watchlist.

## Quality acceptance criteria

### Technical quality

1. All automated tests pass.
2. Repeated runs are identical.
3. Every WARM and HOT score reproduces independently.
4. There are zero schema, wikilink, duplicate-ID, or catalog errors.
5. There are zero production writes.

### Data quality

1. Every included article has a valid date and usable tags.
2. Every missing radar field has a reviewed explanation.
3. There are zero unexplained projection differences.
4. There are zero silently applied low-confidence enrichments.

### Judgment quality

1. Every radar flag receives a disposition.
2. Every surfaced issue has a cited assessment.
3. Every catalyst date has an article citation.
4. Every new or materially escalated issue receives a second review.
5. No cluster relies only on tag-name similarity.

### Accuracy quality

Maintain rolling measurements of surfaced-alert precision, confirmed issues, dismissals, later
discovered misses, reviewer disagreements, average warning lead time, flags assigned to existing
issues, and flags caused by data corrections.

Acceptance requires:

1. Surfaced-alert precision is at least 80%.
2. No known Severe or High benchmark issue is missed.
3. Every maintained benchmark case produces its expected result.
4. Every missed or incorrect case is added to the maintained benchmark set.

## Failure and correction

When a quality gate fails:

1. Mark the run `FAIL` or `CONDITIONAL PASS`.
2. Do not surface unverified issues.
3. Identify whether the cause is source data, enrichment, projection, scoring, or judgment.
4. Correct source data through the normal Markdown and UAT projection process.
5. Rerun using the same evaluation date and a new run ID.
6. Compare the corrected and failed results.
7. Add the failure as a permanent regression or benchmark case.
8. Propose algorithm changes only through a Decision note and a fresh historical backtest.

## Deliverables for every run

1. Run configuration and corpus manifest.
2. Article-field completeness report.
3. UAT radar output and reproducibility hash.
4. Previous-run comparison.
5. UAT-versus-production comparison.
6. Candidate disposition matrix.
7. Judgment and second-review record.
8. Updated issue notes, Issues catalog, and audit log.
9. Quality report marked `PASS`, `CONDITIONAL PASS`, or `FAIL`.
10. Plain-language surfaced-issue report.
