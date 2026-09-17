---
type: goal-contract
name: ai-assisted-enrichment-cascade-goal-contract
status: draft
owner: Christopher (chris@sentient.io)
created: 2026-08-30
applies_to: [Inputs/articles/2026-07, entities/article/2026-07]
---

# AI-Assisted Enrichment and Cascade Goal Contract

This contract defines a reviewable Goal Mode run for enriching the current July input
batch through direct AI-assistant judgment and then cascading the accepted articles
into the main wiki. It does not authorize execution by itself. Execution begins only
after Christopher approves this contract and invokes it as a goal.

The AI assistant performs the enrichment judgments. Scripts may inventory files,
validate decisions, apply reviewed fields, perform cascade bookkeeping, run quality
checks, lint links, and measure performance. They must not replace the AI review with
deterministic classification or call an external model API.

## 1. Objective

Enrich all 723 articles currently under `Inputs/articles/2026-07/`, then compile and
cascade every accepted article into the main wiki while preserving duplicates and
provenance.

For every article, the AI assistant must decide:

1. Issue tags, restricted to active canonical Tag entities under `entities/tag/`.
2. Outlet name.
3. Outlet country.
4. Institutional category.
5. Tone: `Factual` or `Opinionated`.
6. Event type: `Facilitated` or `Unfacilitated`.

No OpenAI API or other external model API may be called. API quota is therefore not a
dependency or breakout condition.

## 2. Starting Boundary

Before enrichment begins:

1. Directly list the real Markdown files in `Inputs/articles/2026-07/`.
2. Freeze a starting manifest containing path, article ID, title, publication date,
   URL, and content hash for every article.
3. Confirm the manifest contains exactly 723 unique paths and article IDs.
4. Record any discrepancy and stop before changing an article.
5. Group exact URL and headline duplicates for consistent review, but retain every
   file as an independent article.

The two previously approved unusable files are already excluded and are not members
of this 723-article boundary.

## 3. AI-Assistant Enrichment

Review the articles in bounded batches, grouping exact duplicates where practical.
For each article, use evidence in this order:

1. Article body and existing frontmatter.
2. The linked source page, opened directly when available.
3. Saved project evidence and prior enrichment artifacts as supporting evidence only.
4. Evidence from an exact duplicate URL or headline.
5. Headline, URL host, and remaining metadata.

An inaccessible source page does not by itself stop the run. Continue through the
fallback evidence order and make a conservative best-judgment classification.

Create a reviewed assessment artifact with one record per manifest article. Each
record must contain:

- Manifest path and article ID.
- The six enrichment decisions.
- Evidence tier and source-access result.
- Confidence for each field.
- Duplicate-cluster identifier, where applicable.
- Concise evidence notes.
- Reviewer identity and review timestamp.

After the first pass, perform a second AI consistency review of:

- Every low-confidence field.
- Every duplicate cluster.
- Conflicting outlet or country assignments.
- Unusually broad or uncommon tag combinations.

Thin evidence, low confidence, and duplicate status must be reported, but are not
breakout conditions by themselves.

## 4. Assessment Validation and Application

Before applying enrichment:

1. Confirm exactly one assessment exists for each manifest article.
2. Reject duplicate or unknown paths and article IDs.
3. Validate all enums and restrict tags to the production inventory.
4. Confirm every target still exists and matches its frozen content hash.
5. Preview the proposed article changes.
6. Confirm the preview touches only the 723 manifest files and only the intended
   enrichment fields.

Apply the reviewed assessment through the existing deterministic assessment-application
mechanism. That mechanism may validate and write the AI assistant's decisions, but it
must not infer, classify, or call a model API.

After application, verify all retained articles contain the six enriched fields and
still reconcile to the starting manifest.

## 5. Cascade and Quality Gates

Run these stages in order:

1. Run `python3 scripts/ingest_cascade.py --month 2026-07 --dry-run`.
2. Inspect destination collisions, schema failures, unresolved links, proposed file
   movements, and the reported article count.
3. If the dry run passes, run
   `python3 scripts/ingest_cascade.py --month 2026-07`.
4. Require the real cascade to compile and move the articles into
   `entities/article/2026-07/`, update entity backlinks and coverage, rebuild
   generated catalogs, append required logs, write its receipt, and complete built-in
   validation.
5. Run `python3 scripts/article_quality.py --check`.
6. Apply only provenance-backed safe repairs, then rerun the quality check.
7. Preview safe link repairs, apply only confirmed mechanical fixes, and run
   `python3 scripts/check_links.py`.
8. Confirm no successfully processed article remains in
   `Inputs/articles/2026-07/`.

Duplicates must remain independent article records. Existing destination articles
must never be overwritten silently.

## 6. Performance Measurement

Measure wall-clock time from immediately before manifest creation until final
reconciliation. Record separate timings for:

- Manifest creation.
- AI evidence gathering and classification, by batch.
- AI consistency review.
- Assessment validation.
- Assessment application.
- Cascade dry run.
- Real cascade, including validation and bookkeeping.
- Article-quality checking and repairs.
- Link linting and repairs.
- Complete end-to-end run.

The final report must state:

- Starting, enriched, cascaded, failed, and excluded article counts.
- Elapsed time for every stage.
- Total elapsed time.
- Average enrichment seconds per enriched article.
- Average cascade seconds per cascaded article.
- Overall average seconds per completed article.
- Slowest stage and any material performance anomalies.
- Paths to assessment artifacts, receipts, and review or exclusion records.

If a stage processes zero articles or aborts, report that explicitly and do not
calculate a misleading average.

## 7. Success End Conditions

The goal succeeds only when:

1. All 723 starting articles are accounted for.
2. Every retained article has an AI-reviewed assessment covering all six fields.
3. No external model API was called.
4. All applied values pass schema and inventory validation.
5. Every retained article has been compiled and cascaded into the wiki.
6. Duplicate inputs remain represented as independent article records.
7. The July input folder contains no successfully processed articles.
8. Entity updates, catalogs, logs, and cascade receipts are complete.
9. Article-quality and link checks have no unresolved hard failures.
10. The final performance report includes all required timings and averages.
11. Every exclusion has an explicit reason and Christopher's approval.

## 8. Breakout Conditions

Stop before proceeding to the next stage and report the affected files when:

- The frozen 723-file manifest changes after processing begins.
- Paths or article IDs collide, or an article lacks essential identity fields.
- The production tag inventory or article schema is missing or inconsistent.
- No available evidence supports a meaningful best-judgment value for a required
  field.
- Evidence contains an irreconcilable provenance conflict.
- Assessment application would alter a file outside the manifest, change unintended
  content, or violate the article schema.
- Cascade dry-run reports destructive changes, destination conflicts, or problems
  requiring editorial judgment.
- The real cascade fails, leaves partial movements, or cannot complete required
  bookkeeping and validation.
- Quality or link checks expose a non-mechanical problem requiring judgment.
- An article appears unusable and would require deletion or exclusion.

For a breakout, preserve all completed artifacts, record elapsed time and completed
counts, identify the exact stopping condition, and wait for Christopher's decision.
Do not delete, exclude, overwrite, loosen schemas, invent tags, or bypass a gate.

## 9. Goal Invocation

After this contract has been reviewed and approved, invoke Goal Mode with:

> Enrich and cascade the complete frozen July article batch according to
> `scripts/ai_assisted_enrichment_cascade_goal_contract.md`. Perform enrichment
> through direct AI-assistant judgment without calling an external model API, retain
> duplicates, enforce every quality gate and breakout condition, time every stage,
> and finish only when all success end conditions are satisfied or a documented
> breakout requires Christopher's decision.
