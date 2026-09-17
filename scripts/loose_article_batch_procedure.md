---
type: procedure
name: loose-article-batch
status: active
last_updated: 2026-08-18
---

# Loose Article Batch Procedure

Use this procedure every time a crawler lands loose Markdown article files directly under
`Inputs/articles/`. It converts a moving intake pile into a bounded batch, routes those files into
monthly folders, enriches their radar/input fields, ingests and cascades them into the wiki, then
runs quality and lint gates.

This procedure is reusable. Each run must create its own starting manifest and performance summary
so later optimisation can compare batches cleanly.

## Objective

Process the loose article files directly under `Inputs/articles/` through the full route -> enrich
-> ingest/cascade -> article quality -> lint pipeline, while timing every major stage.

## Reusable batch boundary

Each run begins by freezing a fresh manifest of real article files directly under
`Inputs/articles/`, excluding system metadata such as `.DS_Store`.

The manifest defines the batch. Any loose files that appear after the manifest is frozen belong to
the next batch unless Christopher explicitly approves adding them to the current run. Do not silently
mix new crawler arrivals into an active batch.

## Success end conditions

The run is complete only when all of these are true:

1. Every file in the starting manifest is routed, cascaded, explicitly excluded, or listed as a
   breakout item.
2. No accepted starting-manifest file remains loose under `Inputs/articles/`; documented
   source-empty and enrichment holds remain under Inputs unchanged in content.
3. Exact source-empty stubs have been placed on documented hold, and enrichment has been run and
   applied where defensible under the approved policies to the eligible subset, including the
   bounded remediation loop for initial failures.
4. Review-required enrichment cases are resolved or reported.
5. Every approved/touched month has been cascade-processed.
6. Article quality and lint gates have no unresolved hard failures.
7. The exact approved UAT bundle has been loaded and post-load parity verified, with no production
   write.
8. A performance summary exists with timings, counts, throughput, failures, and receipt/artifact
   paths for every stage.

### Outlet-aware duplicate acceptance

Apply [[accept-cross-outlet-duplicate-coverage]] before excluding any duplicate. Duplicate flags,
substantially repeated text, shared wire copy, or equivalent coverage are review signals only. When
saved evidence verifies a different publishing outlet, keep the item eligible as an independent
provenance record and process it through enrichment, compile, cascade, and UAT projection.

Verify the publishing outlet in this order: saved `publisherDomain` or normalized article URL
hostname; an existing canonical outlet identity or alias; then a saved publisher name supported by
source metadata. A source quoted or credited in the body is not the publishing outlet. `Unknown
Outlet`, a blank value, or an outlet-label mismatch does not establish a different outlet.

Hold an arrival as already compiled only when it is a same-outlet repeat of the same source record,
normally shown by the same upstream article ID or exact canonical URL plus matching title, date, and
source evidence. A different-outlet article must retain its distinct upstream ID and source URL. If
it collides with an existing ID or compiled path, break out rather than overwrite or fabricate an
identity.

## Goal-mode breakout conditions

When this procedure is run as a long-running Codex goal, pause and report a breakout instead of
continuing if any required stage is blocked by approval, unavailable credentials, ambiguous review
findings, or non-mechanical repair.

The common breakout cases are:

1. Enrichment needs model/API access, source-page fetching, or external data transfer that has not
   been approved for the current run.
2. The bounded AI enrichment-remediation loop has completed two cycles and still leaves required
   fields without a defensible saved-evidence classification, or the required AI/runtime access is
   unavailable. An initial incomplete result is not itself a breakout.
3. Routing finds an unroutable eligible file, invalid `publishedDate`, destination collision, or an
   already-compiled conflict that remains unresolved after the outlet-aware duplicate audit. Exact
   source-empty stubs and verified same-outlet repeat arrivals are holds, not routing failures.
4. Cascade exits nonzero, leaves starting-manifest files behind, or reports focused validation hard
   errors.
5. Article quality finds issues that cannot be safely repaired from preserved provenance.
6. Lint finds unresolved broken links, YAML errors, ambiguous aliases, or entity-linking work that
   requires judgment.

For every breakout, report the stage, command, elapsed time so far, affected file count, sample
files/findings, and the safest next decision Christopher needs to make.

## Performance summary requirement

Time every major stage, including dry runs and apply runs when both are used. The final report must
include a table with this shape:

| stage | command | start | end | elapsed sec | input count | success/output count | failure count | throughput | receipt/artifact |
|---|---|---|---|---:|---:|---:|---:|---:|---|

Report these headline metrics after the run:

1. Total end-to-end elapsed time.
2. Total starting-manifest articles.
3. Total successfully cascaded articles, same-outlet repeat holds, different-outlet duplicates
   accepted, and unresolved publisher-review holds.
4. End-to-end seconds per article.
5. Routing files per second.
6. Enrichment articles per second.
7. Cascade seconds per article by month and overall.
8. Article quality notes per second.
9. Lint notes per second.
10. Slowest stage by elapsed time.
11. Slowest stage by per-article throughput.
12. UAT approval, load, and post-load verification timings and row counts.

## Step 1 - Freeze and time batch inventory

1. Start the end-to-end timer before inventory.
2. Fresh-count direct files under `Inputs/articles/`, excluding `.DS_Store`.
3. Save the master starting manifest with paths, IDs, titles, publication dates, and SHA-256 hashes.
4. Classify as `source-empty` only a file with an article ID but empty title, publication date, URL
   and body plus an empty `rawNewsApiResponse`. Move it without content changes to
   `Inputs/articles/holds/<run-id>/` and list it with the reason in a hold manifest.
5. Build the eligible manifest from every remaining starting file. The current audit found 38
   source-empty stubs, but every run must count them freshly rather than hard-code that number.
6. Require master count = eligible count + source-empty hold count, then record elapsed time, loose
   file count, and ignored metadata count.

Break out if the intake changes before the manifest is frozen.

## Step 2 - Route loose files

After source-empty holds have left the loose root, run and time the dry run over the remaining
eligible files:

```bash
python3 scripts/route_input_articles.py
```

If every eligible file is routable, run and time the apply step:

```bash
python3 scripts/route_input_articles.py --write
```

Record files routed, skipped, blocked, invalid dates, same-outlet repeat holds, different-outlet
duplicates accepted, unresolved publisher identities, and files per second.

For every already-compiled conflict, compare saved publisher evidence before disposition. Move a
verified same-outlet repeat unchanged to the run-specific hold folder and record the matched compiled
path and identity evidence. Keep verified different-outlet coverage eligible. Break out if any
accepted file is unroutable, has a missing or invalid `publishedDate`, would overwrite a destination,
has unresolved publisher identity, or has a different-outlet source-ID/path collision. Expected
source-empty and verified same-outlet duplicate holds do not fail routing and must not be filled from
model background knowledge.

## Step 3 - Enrich routed inputs

Run enrichment before cascade so the compiled article can preserve enriched issue/radar fields.
Pass the frozen eligible manifest to every enrichment command: after routing, a month folder can
contain earlier intake files that are not part of the active batch.

When Christopher selects in-app assistant review instead of external API enrichment, run two
independent Codex reviews over the saved input evidence, reconcile them with
`reconcile_local_enrichment_reviews.py`, and apply the resulting assessment through
`enrich_radar_inputs.py --apply-assessment`. Do not fetch pages or call an external model API.

Apply the accepted sentiment policy from
`entities/decisions/adopt-ai-sentiment-policy-and-third-reviewer.md` in both reviews. If the two
reviews disagree or fall below confidence on sentiment, run a third independent in-app Codex
reviewer over exactly that subset. The third reviewer must use only saved evidence and return one
final `Positive`, `Neutral`, or `Negative` decision, confidence, a short verbatim excerpt, and the
numbered policy rule used. Genuine ambiguity resolves to `Neutral`; no human approval is required.

Apply the accepted tone policy from
`entities/decisions/adopt-ai-tone-policy-and-third-reviewer.md`. For every Factual-versus-
Opinionated disagreement, run a third independent in-app AI reviewer over exactly that subset.
Each decision must return `Factual` or `Opinionated`, confidence, a short literal saved-evidence
excerpt, and rationale. The complete body outranks headline style; attributed opinion and narrative
style alone remain Factual; material publication-owned judgment makes the article Opinionated; and
ambiguity defaults to Factual.

Apply the accepted metadata and event-type policy from
`entities/decisions/adopt-ai-metadata-and-event-adjudication-policy.md`. For each metadata or
event-type disagreement, run a third independent in-app AI reviewer over exactly that subset.
Metadata decisions must return the publishing outlet, stable outlet ID, country (blank is allowed),
institutional category, source basis, any stated original agency, confidence, saved evidence, and
rationale. Event decisions must return `Facilitated` or `Unfacilitated`, the publication trigger,
confidence, saved evidence, and rationale. No human approval is required.

Pass the complete adjudication artifacts into reconciliation:

```bash
python3 scripts/reconcile_local_enrichment_reviews.py \
  --manifest-evidence runs/YYYY-MM-DD/artifacts/<batch>-manifest.json \
  --primary runs/YYYY-MM-DD/artifacts/<batch>-primary-review.json \
  --review runs/YYYY-MM-DD/artifacts/<batch>-secondary-review.json \
  --tone-adjudication runs/YYYY-MM-DD/artifacts/<batch>-tone-adjudication.json \
  --sentiment-adjudication runs/YYYY-MM-DD/artifacts/<batch>-sentiment-adjudication.json \
  --metadata-adjudication runs/YYYY-MM-DD/artifacts/<batch>-metadata-adjudication.json \
  --event-adjudication runs/YYYY-MM-DD/artifacts/<batch>-event-adjudication.json \
  --output runs/YYYY-MM-DD/artifacts/<batch>-reconciled-enrichment.json \
  --accepted-manifest runs/YYYY-MM-DD/artifacts/<batch>-accepted-manifest.txt \
  --held-manifest runs/YYYY-MM-DD/artifacts/<batch>-held-manifest.txt \
  --hold-output runs/YYYY-MM-DD/artifacts/<batch>-review-holds.json
```

Each adjudication file must cover every and only its corresponding disagreed subset. It clears only
that domain's hold and an explicit reviewer request whose reason is wholly resolved by the same
decision. It cannot clear an unrelated tone, sentiment, event, metadata, or reviewer hold.

Time the preview:

```bash
python3 scripts/enrich_radar_inputs.py --manifest runs/YYYY-MM-DD/artifacts/<batch>-manifest.txt --no-fetch
```

Time the normal assessment:

```bash
python3 scripts/enrich_radar_inputs.py --manifest runs/YYYY-MM-DD/artifacts/<batch>-manifest.txt
```

Time the apply step:

```bash
python3 scripts/enrich_radar_inputs.py --manifest runs/YYYY-MM-DD/artifacts/<batch>-manifest.txt --apply
```

Only `readyForCascade` assessments are applied. Other articles are retained under Inputs and listed
in the review-hold artifact. Run the complete-input gate against the same manifest before cascade:

```bash
python3 scripts/enrich_radar_inputs.py --manifest runs/YYYY-MM-DD/artifacts/<batch>-manifest.txt --check-complete
```

### Automatic AI enrichment remediation

If the complete-input gate finds any incomplete or invalid selected article, do not immediately
hold it and do not call cascade. Run a bounded AI remediation loop over only the failed subset:

1. Produce a field-level diagnostic for every failed article, separating missing values, invalid
   enums, disagreement/low confidence, publisher identity, and insufficient source evidence.
2. Re-examine the saved article body, saved `rawNewsApiResponse`, saved publisher metadata, and
   existing canonical wiki entities. Apply the accepted sentiment, tone, metadata, and event-type
   policies. Do not use model background knowledge as evidence and do not fetch live sources unless
   that external transfer was already authorized for the run.
3. Invoke the field-specific third AI automatically for every unresolved two-review disagreement or
   low-confidence judgment. Its output must include the final value, confidence, literal saved
   evidence, policy rule, and rationale; no human approval is required.
4. Apply only defensible evidence-backed corrections, then rerun the shared complete-input gate over
   the failed subset and rebuild the accepted and held manifests.
5. Permit at most **two remediation cycles after the initial failed gate**. Record each cycle's
   inputs, field findings, decisions, changed files, remaining failures, timings, and artifacts.
6. When saved evidence supports a valid classification, retain the best policy-compliant AI
   decision with its confidence and rationale even if the first two reviewers did not agree. Never
   lower the structural completeness gate or invent evidence merely to make an article pass.
7. If a file meets the exact source-empty definition, move it unchanged to the source-empty hold.
   If it contains some source material but still cannot support a required field after both cycles,
   place it on a distinct `enrichment-source-insufficient` hold with the unresolved fields and
   evidence inspected.

Only the accepted subset that passes the shared contract may proceed. `ingest_cascade.py` enforces
the same contract batch-wide before mutation; a cascade rejection returns the exact failed subset to
this remediation loop and never authorizes cascade defaults.

Record files assessed, high-confidence fields applied, low-confidence cases, classifier
disagreements, failures, articles per second, and artifact paths.

Break out if required API/model access is unavailable or an unapproved external transfer is the only
way to obtain missing evidence. After two remediation cycles, remaining unsupported classifications
become documented `enrichment-source-insufficient` holds; continue with the accepted manifest subset
unless no articles are ready.

## Step 4 - Confirm the final projection and load gates

Do not stage enriched inputs directly into UAT. Compiled Markdown is the database source of truth,
so projection happens only after compile and cascade.

Confirm that every real cascade in Step 6 is local-only and cannot call projection or database
tools. After all selected months have cascaded and Steps 7-8 pass, Step 9 prepares and verifies one
hashed final UAT bundle through `scripts/project_wiki_to_uat.py`. That final stage may allocate
stable UAT identities in article projections. Loading occurs only after the exact final bundle is
verified and approved.

Record bundle ID, inserts, updates, deletes, verification status, elapsed time, and average seconds
per article. Break out on any proposed delete, unexpected update, identity mismatch, failed
verification, or production target. After all gates pass, the Goal agent creates the required
attributed approval for the exact verified bundle under Christopher Yeo's standing authorization
dated 2026-08-14 and continues without a human pause.

## Step 5 - Identify touched months

List the `Inputs/articles/YYYY-MM/` folders that contain files from the starting manifest after
routing and enrichment. Record elapsed time and article count per month.

Process one month at a time so cascade failures and throughput are attributable.

## Step 6 - Cascade each month

For each touched month, time the optional dry run:

```bash
python3 scripts/ingest_cascade.py --month YYYY-MM --manifest runs/YYYY-MM-DD/artifacts/<batch>-manifest.txt --dry-run
```

Then time the real run:

```bash
python3 scripts/ingest_cascade.py --month YYYY-MM --manifest runs/YYYY-MM-DD/artifacts/<batch>-manifest.txt
```

For every real cascade run, report total elapsed time, articles processed, average seconds per
article, focused validation errors, inputs remaining, and receipt path. Confirm that no projection
or database command ran during the cascade.

Break out if the cascade exits nonzero, focused validation has hard errors, a month folder still
contains starting-manifest files after a supposedly complete run, catalog rebuild fails, or status
bookkeeping fails.

## Step 7 - Article quality gate

Time the quality check:

```bash
python3 scripts/article_quality.py --check
```

If the report identifies safe provenance-backed repairs, time the repair preview:

```bash
python3 scripts/article_quality.py --fix-safe --dry-run
```

Apply only clean, provenance-backed safe repairs. Record notes checked, findings, repairs applied,
residual findings, elapsed time, and notes per second.

Break out if sentiment overrides are needed, provenance is ambiguous, duplicate IDs appear, article
month placement is wrong, or any repair would require rewriting body text or making a judgment call.

## Step 8 - Lint and safe repair

Time the lint/fix preview:

```bash
python3 scripts/fix_links.py --dry-run
```

If findings are mechanical safe repairs, time the actual repair:

```bash
python3 scripts/fix_links.py
```

Then time the report-only confirmation:

```bash
python3 scripts/check_links.py
```

Record notes scanned, hard failures, warnings, repairs applied, remaining findings, elapsed time,
notes per second, and receipt paths.

Break out if unresolved broken links, YAML errors, ambiguous aliases, or judgment-required entity
linking remain.

## Step 9 - Load and verify UAT

After every cascade, article quality, and link check passes:

1. Prepare exactly one final bundle with `project_wiki_to_uat.py prepare-current`, verify it, apply
   its projection backfills to local Markdown, verify it again, and run the live UAT diff. Record
   its immutable bundle ID, hashes, inserts, updates, and deletes.
2. Do not pause for human approval. Create the loader's attributed approval file only now, containing
   `approved: true`, that exact `bundleId`, the current `approvedAt`, `approvedBy: Codex Goal agent
   under Christopher Yeo's standing authorization dated 2026-08-14`, and the originating user
   instruction as `approvalSource`.
3. Immediately run:

   ```bash
   python3 scripts/project_wiki_to_uat.py load \
     --bundle-dir <verified-bundle> \
     --approval-file <approval-file>
   ```

   The target must be `AI_Animation_UAT`; production is prohibited.
4. Run the post-load diff and bundle verification. Require exact compiled-Markdown-to-UAT parent
   and child-multiset parity for the approved scope.
5. Preserve the approval evidence and load receipt. On transaction failure, verify rollback and no
   partial database changes; on any parity difference, report failure and do not claim completion.

## Step 10 - Final reconciliation

Time the final counts and summary.

Record:

1. Starting manifest count.
2. Routed count.
3. Enriched count.
4. Cascaded count.
5. Source-empty hold count, remediation-cycle counts, enrichment-source-insufficient/review hold
   count, and any other breakout count.
6. Loose starting-manifest files remaining.
7. Monthly input files remaining for touched months.
8. Compiled article totals.
9. Enrichment status.
10. Article quality status.
11. Lint status.
12. Verified UAT bundle ID and delta counts.
13. Attributed approval evidence, UAT load receipt, and post-load parity result.
14. All relevant run receipts and artifacts.

Stop the end-to-end timer only after final reconciliation is complete.

## Hard breakout rule

Pause instead of guessing whenever the pipeline needs external data-transfer approval, enrichment
judgment outside the accepted AI policies, source interpretation that remains unsupported after two
remediation cycles, schema changes, manual entity creation, or non-mechanical repair. Do not pause
for evidence-backed AI remediation or third-AI adjudication within the accepted policies.
Do not pause for UAT-load approval: after every exact-bundle safety gate passes, create the attributed
automated approval and continue. Any unresolved bundle or database gate remains a breakout.
