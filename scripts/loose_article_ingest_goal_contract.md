---
type: goal-contract
name: loose-article-ingest-goal-contract
status: active
owner: Christopher (chris@sentient.io)
created: 2026-08-13
last_updated: 2026-08-18
applies_to: [Inputs/articles, entities/article, UAT delta preparation, UAT load]
---

# Loose-Article Enrichment and Cascade Goal Contract

## Objective

Process one frozen loose-input batch through route, two-pass enrichment, review,
manifest-scoped compile/cascade, quality and link gates, and verified UAT-delta
preparation. After the exact bundle passes every gate, create its attributed automated approval,
load UAT immediately, and verify parity. Never write production in this goal.

## Batch boundary

1. Freeze direct Markdown files under `Inputs/articles/`, excluding `.DS_Store`.
2. Record path, filename, article ID, title, publication date, and SHA-256.
3. Later arrivals are outside the run. A changed hash is a breakout.
4. Preserve one master manifest for every starting file, plus separate eligible and hold manifests.

## Source-empty hold rule

Before routing, identify files that contain an article ID and classification shell but have no
usable source evidence: empty title, publication date, URL and body, together with an empty
`rawNewsApiResponse`. Place each such file on a documented `source-empty` hold, moving it without
content changes to `Inputs/articles/holds/<run-id>/`. It must not block eligible articles, be
enriched from model background knowledge, be ingested, be cascaded, or enter the UAT bundle.

The current audit found 38 such stubs, but every Goal run must determine the count freshly rather
than hard-code 38. A partially populated file that does not meet this exact source-empty definition
enters the bounded AI enrichment-remediation loop; it is not an automatic source-empty hold or an
immediate breakout.

## Outlet-aware duplicate rule

Follow [[accept-cross-outlet-duplicate-coverage]]. Duplicate flags, repeated text, shared wire copy,
and equivalent coverage are review signals, not exclusions. If saved evidence verifies a different
publishing outlet, retain the item as an independent provenance record and process it normally.

Verify the publisher first from `publisherDomain` or the normalized article URL hostname, then from
an existing canonical outlet identity or alias, and finally from a saved publisher name supported by
source metadata. A source quoted or credited in the body is not the publishing outlet. `Unknown
Outlet`, a blank value, or an outlet-label mismatch does not prove that outlets differ.

An already-compiled duplicate hold is permitted only for a same-outlet repeat of the same source
record, normally established by the same upstream article ID or exact canonical URL together with
matching title, date, and source evidence. A verified different-outlet item must retain a distinct
upstream identity and source URL. If it collides with an existing source ID or compiled path, break
out for provenance-safe resolution; never overwrite a compiled article or fabricate an ID.

## Required sequence

1. Classify exact source-empty stubs, write the hold manifest and reasons, and create the eligible
   manifest from the remaining files. Require master count = eligible count + source-empty hold
   count.
2. Dry-run routing against the eligible manifest. Audit every already-compiled conflict under the
   outlet-aware duplicate rule: move verified same-outlet repeat arrivals unchanged to a documented
   hold, keep verified different-outlet coverage eligible, and hold unresolved publisher identity
   for review. Then route only if every accepted article is routable. Expected source-empty and
   verified same-outlet duplicate holds do not fail this gate.
3. Run two independent enrichment reviews with the eligible manifest. They may use the
   configured enrichment API only with explicit data-transfer approval, or two
   isolated in-app Codex reviewers using saved local evidence only. Both passes
   must agree above the configured confidence threshold on tone, event type, outlet,
   outlet country, and institutional category. For sentiment, both passes apply the accepted policy
   in `entities/decisions/adopt-ai-sentiment-policy-and-third-reviewer.md`. Any remaining sentiment
   disagreement or low-confidence result goes to a third independent AI reviewer, whose complete
   saved-evidence-only adjudication is final for sentiment and requires no human approval. Metadata
   and event type follow `entities/decisions/adopt-ai-metadata-and-event-adjudication-policy.md`;
   each remaining disagreement likewise goes to a third independent AI reviewer for a complete,
   final, saved-evidence-only decision over exactly that field's subset.
   Factual-versus-Opinionated tone follows
   `entities/decisions/adopt-ai-tone-policy-and-third-reviewer.md`; each remaining tone disagreement
   likewise goes to a third independent AI reviewer, with ambiguity defaulting to `Factual`.
4. Apply only `readyForCascade` assessments. Complete inputs contain category,
   topic, tone, toneSentiment, eventType, tags, outlets, countries,
   coverageCount, mediaCount, sourceType, and URL. Crawler inputs use
   `sourceType: crawl`.
5. Treat relevance and duplicate flags as review signals, not automatic exclusions. Retain usable
   duplicates from verified different publishing outlets as independent provenance records; hold
   only verified same-outlet repeats of the same source record.
6. Run the shared complete-input gate. For any failure, automatically run up to two AI remediation
   cycles over only the failed subset before creating an enrichment hold. Each cycle must diagnose
   the exact failed fields; inspect the saved article body, `rawNewsApiResponse`, publisher metadata,
   and canonical wiki entities; apply the accepted field policies; invoke a field-specific third AI
   for unresolved disagreement or low confidence; apply defensible corrections; and rerun the gate.
   Record confidence, literal saved evidence, policy rule, rationale, changes, remaining failures,
   timing, and artifacts. No human approval is required. Never use model background knowledge as
   evidence or make an unapproved live fetch. A file matching the exact source-empty definition goes
   unchanged to the source-empty hold. A partially evidenced article still unsupported after both
   cycles goes to an `enrichment-source-insufficient` hold. Each decision clears only its own domain;
   it never overrides an unrelated gate or lowers the shared cascade-ready contract.
7. For each touched month, run `ingest_cascade.py --dry-run --manifest ...`, then
   the real manifest-scoped local-only cascade. Never process unrelated monthly files. The cascade
   must not call projection or database tools.
8. Run article quality and link gates. Permit one mechanical link-repair attempt;
   stop on judgment-required repair.
9. Only after every accepted article has cascaded and all batch-wide quality/link gates pass,
   prepare exactly one final UAT bundle for the complete frozen batch. Run `prepare-current`,
   `verify-bundle`, `apply-projections` to local Markdown, `verify-bundle` again, and `diff` as
   separate commands. Require no unexpected update, delete, identity mismatch, or production
   target. Record its immutable bundle ID, hashes, inserts, updates and deletes.
10. **Automated approval:** do not pause for human approval. Only after Step 9 passes, create the
    loader's required attributed approval file with `approved: true`, the exact verified `bundleId`,
    the current `approvedAt`, and `approvedBy: Codex Goal agent under Christopher Yeo's standing
    authorization dated 2026-08-14`. Record the originating user instruction as
    `approvalSource`. Never create this file before the bundle exists or if any gate is unresolved.
11. Immediately run `project_wiki_to_uat.py load` with the exact bundle and automated approval
    file. The tool must re-verify the bundle and live diff immediately before its atomic,
    rollback-on-failure transaction. The target must be `AI_Animation_UAT`; production is prohibited.
12. Run the post-load diff and bundle verification. Require exact compiled-Markdown-to-UAT parent
    and child-multiset parity for the approved scope, preserve the load receipt, and verify that a
    failed transaction made no partial database changes.

## Success conditions

- Starting count equals cascaded plus source-empty holds plus verified same-outlet duplicate holds
  plus enrichment/publisher-review holds.
- Every cascaded article passes the complete-input gate before cascade and the
  compiled article-quality gate afterward.
- Every initial enrichment failure has either passed within two documented AI remediation cycles or
  has a specific evidence-backed hold reason; no article is held merely because the first attempt
  was incomplete.
- All accepted manifest files move to `entities/article/YYYY-MM/`; held files
  remain under Inputs.
- Link checks have no hard failures.
- Exactly one final batch UAT bundle is prepared after all cascades and local quality gates pass.
- The approved UAT bundle loads successfully and the post-load parity check has no unexplained
  parent or child differences.
- The automated approval artifact and UAT load receipt identify the exact bundle, executing agent,
  timestamp, and standing authorization; no production load or production write occurs.
- Final reporting includes timings, throughput, counts, touched months,
  assessment/hold artifacts, cascade receipts, UAT bundle paths, approval evidence, load receipt,
  and post-load verification.

## Breakouts

Stop on manifest drift, missing credentials, model failure that prevents the remediation loop,
required fields or provenance still unsupported after two remediation cycles, an unresolved
publisher identity, a different-outlet source-ID/path collision, routing/cascade collision, non-mechanical
quality or link failure, UAT delete/unexpected update, identity mismatch, verification failure,
inability to create a correctly attributed exact-bundle approval, production target, transaction
failure, or post-load parity failure. Preserve completed artifacts and report exact affected
articles.

## Goal invocation

> Execute `scripts/loose_article_ingest_goal_contract.md` against the current loose-article batch
> and achieve all its objectives and end conditions.
