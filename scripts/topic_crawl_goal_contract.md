---
type: goal-contract
name: autonomous-topic-crawl
status: active
created: 2026-09-15
owner: Christopher Yeo
---

# Autonomous Topic Crawl Goal Contract

This contract makes `scripts/topic_crawl_plan.md` safe to run as a durable Goal
Mode objective. It authorizes the normal local crawl lifecycle without routine
handoffs. It does not authorize production writes, schema changes, secret
disclosure, source-text invention, or action outside the vault.

## Goal objective

> Close the specified Topic Crawl batch autonomously. Use
> `scripts/topic_crawl_plan.md` and this contract. Process topics in batches of
> ten. For every discovered candidate, retain source evidence and write exactly
> one terminal disposition: `cascaded`, `duplicate`, `off-topic`, `rejected`,
> or `held`. Apply the approved resolution policy. Resume from persisted state
> after interruption. Do not ask for routine relevance, enrichment, retrieval,
> duplicate, or hold decisions. Finish only after exact reconciliation,
> validation, topic checkpoint verification, and a final receipt. Escalate only
> a defined critical event.

## Durable state

At the start of each ten-topic batch create a fresh run folder:

```bash
python3 scripts/topic_crawl_goal_runner.py init \
  --run-dir runs/YYYY-MM-DD/artifacts/topic-crawls/<run-label> \
  --topic <canonical-topic-id> [--topic <canonical-topic-id> ...] \
  --date-start YYYY-MM-DD --date-end YYYY-MM-DD
```

The folder has `goal-state.json` (scope, retry limits, policy, checkpoints, and
per-month cascade receipts), `candidates.ndjson` (append-only evidence ledger),
and `goal-receipt.json` (written only after closure). Each ledger record needs
`candidateId`, `topicId`, `set`, `canonicalUrl` or `providerUri`, `disposition`,
and a source-backed `reason`. A `cascaded` record also needs `intakePath`,
`articlePath`, and non-empty `phaseEvidence` for `identity`, `date`,
`completeness`, `relevance`, `normalized`, `enriched`, `dryRun`, `cascaded`,
`topicLinked`, and `validated`.

The ledger is the resume boundary. Never re-fetch or re-enrich a candidate that
already has a terminal disposition. The target topic's `## Coverage` block must
link every cascaded article. Every affected month needs valid dry-run and real
`ingest_cascade` receipts in `goal-state.json.monthReceipts`; a moved article is
not a substitute for a real timed receipt.

Persist receipts immediately after each real cascade:

```bash
python3 scripts/topic_crawl_goal_runner.py record-month-receipts \
  --run-dir <run-dir> --month YYYY-MM \
  --dry-run-receipt runs/YYYY-MM-DD/<dry-run-receipt>.json \
  --cascade-receipt runs/YYYY-MM-DD/<real-receipt>.json
```

## Autonomous operating rules

1. Run Set A relevance before Set B discovery and Set B URL relevance before mapping or retrieval.
2. Apply `schemas/topic_crawl_resolution_policy.yaml`; inconclusive source-body evidence is `held`.
3. Use at most three attempts per transient request with configured backoff, then record `held`.
4. Enforce the independent 500-item ceiling for Set A and Set B per topic.
5. Persist the ledger after every disposition and reconcile at each topic boundary.
6. Cascade only policy-complete source-backed inputs, dry-preview first, validate each month after
   real cascade, and verify or safely repair the intended topic Coverage backlink.
7. Record a topic completion checkpoint only after its governed status procedure and catalog
   validation succeed.
8. Close only through `reconcile` and `close` in `topic_crawl_goal_runner.py`.

## Critical events requiring a handoff

Only seek direction for an unresolved credential or required-discovery failure,
systemic provider failure, provenance/manifest conflict, required schema/rule
change, unauthorized/destructive/production action, or unrecoverable cascade or
validation failure. Individual off-topic results, duplicates, publisher blocks,
missing bodies, exhausted per-item retry budgets, low-confidence enrichment, and
normal holds are not critical: record them and continue.

## Completion standard

The goal is complete only when every selected topic has a verified completed
checkpoint, every ledger row has one terminal disposition, every cascaded row
has an existing article path with complete phase evidence and a target-topic
Coverage backlink, each affected month has valid dry-run and real cascade
receipts, there are no unresolved critical events, and `goal-receipt.json` exists.
