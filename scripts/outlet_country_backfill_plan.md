---
type: plan
name: outlet-country-backfill
status: proposed
last_updated: 2026-08-27
---

# Outlet Country Backfill and Stub Repair Plan

Restores `country` — and the rest of the frozen outlet schema — on the 1,617 outlet notes created by
the crawl intake path without it, using the resolution ladder already approved in
[[adopt-ai-metadata-and-event-adjudication-policy]] rather than a new policy.

**Default posture:** every tier produces a review table first. No note is written until the user asks
for the update pass or accepts the proposed table, matching
`scripts/people_country_inference_procedure.md`.

## Problem statement

As of 2026-08-27, `entities/outlet/` holds 2,080 notes. 463 carry a `country`; **1,617 do not** —
and the gap is wider than one field:

| Defect | Count |
|---|---:|
| No `country` key at all (not blank — absent) | 1,617 |
| No `mediaCategory` key | 1,617 |
| No `channels` key | 1,617 |
| No `outletId` key | 1,617 |
| `displayName` is a raw slug, not a readable name | 1,615 |

These notes were not created from the outlet Template in `entities/outlet/index.md`. Every one of
them is crawl-sourced; every feed-sourced outlet has its country, because the feed export carries
`country` and `mediaOutletCategory` per coverage record while the crawler payload's publisher fields
were dropped at compile time.

**Consequences.** The Outlets domain's `## Producing a List` convention buckets 78% of the domain
into "(Country unknown)". Any country-cut analysis silently answers over the feed corpus only — as
the 2026-08-27 query on foreign coverage of Southeast Asia animation did. Lint cannot check `country`
wikilink integrity on notes that have no such field.

## Evidence ladder

No live web enrichment. The ladder below is [[adopt-ai-metadata-and-event-adjudication-policy]]
items 1-4 applied retroactively, in the same priority order:

1. saved `publisherCountry`
2. saved `publisherLocation` where it identifies a country
3. `rawNewsApiResponse.source` country
4. country-code domain
5. AI inference from the outlet's known identity — **review-gated, never auto-written**
6. still uncertain → leave blank, and record why (policy item 4: not a hold)

An existing canonical wiki outlet name and country override any new inference (policy item 2).

## Tiers and expected yield

| Tier | Method | Outlets | Confidence |
|---|---|---:|---|
| T0 | Recover saved publisher metadata from `Inputs/articles/` and the 27 `runs/*/artifacts/**` adjudication files | 349 | High — source-provided |
| T1 | ccTLD on the article's `sourceUrl` (`.uk`, `.au`, `.in`, …) | 298 | High |
| T2 | US broadcast/local slug (call sign `W*`/`K*`, state abbreviation or state name in the outlet name) | 87 | Medium-high |
| T3 | Remainder — gTLD, no saved metadata | 883 | Requires review |
| | **Total** | **1,617** | |

T0-T2 close **734 of 1,617 (45%)** on source-provided or deterministic evidence.

**Cross-validation already run.** On the 116 domains where a saved `publisherCountry` and a ccTLD
both exist, the two agree on 115 (99.1%). The single disagreement — `finanznachrichten.de` saved as
Switzerland — is a corporate-domicile-vs-ccTLD split, not a data error. This is the evidence that
the ladder's top and fourth rungs are consistent enough to run unattended for T0/T1.

**Known failure mode.** Provider `publisherCountry` reflects where the publisher is registered, not
its editorial home. `irishsun.com` resolves to Australia (a Big News Network property). T0 therefore
still needs a sampled spot-check, not blind trust — see the gates below.

## Execution

### Phase 1 — Schema repair (mechanical, no inference)

Bring all 1,617 notes onto the frozen Template: add `outletId`, `country`, `mediaCategory`,
`channels`, keep `articleCount` and `aliases` as-is. `country` is added **empty** in this phase.
Slug `displayName` values stay untouched here — renaming is Phase 4.

This phase is deterministic and reversible, and should land before any country write so that later
passes edit a conforming field rather than inserting one.

### Phase 2 — T0/T1 backfill

New script `scripts/outlet_country_backfill.py`, modelled on `scripts/people_country_inference.py`:

- `--propose` (default) writes a review table to `runs/<date>/artifacts/` — one row per outlet with
  the resolved country, the ladder rung that produced it, and the citing evidence (input file path or
  article `sourceUrl`).
- `--apply` writes only rows the user has accepted, through `scripts/patch_coverage.py`-style safe
  patching, and appends `entities/outlet/log.md`.
- Every write records its ladder rung, so a later pass can retire a rung-4 value when a rung-1 value
  arrives.

### Phase 3 — T2, then T3

T2 (87) runs the same script with the broadcast-slug rule enabled and a **full** review table — the
rule is a heuristic on the outlet's own name, not source metadata, so it does not inherit T0/T1's
unattended posture.

T3 (883) is the review-gated tier. Work it in bounded batches ordered by `articleCount` descending,
so the outlets that actually move analysis get resolved first. Note that the tail is genuinely thin:
the blank set accounts for 4,598 outlet-article links across 1,617 outlets — a mean below three
articles each. **Resolving the top ~150 by volume likely recovers most of the analytical value**;
the long tail can sit blank under policy item 4 indefinitely without harm.

### Phase 4 — Identity and merge queue

Separate from country, and worth doing while the domain is open:

- **9 outlets** normalise to the same identity as an existing filled outlet — straight duplicates to
  merge (inherit the canonical note's country per policy item 2).
- **65 more** are prefix-overlap candidates and must be reviewed one by one, never auto-merged:
  `the-sun` (UK) vs `the-sun-daily` (Malaysia), `cnbc` vs `cnbc-indonesia`, and
  `line-today` vs `line-today-thailand` are distinct outlets that look like duplicates.
- Slug `displayName` values get their readable names from saved `publisherName` where T0 recovered
  one, otherwise from AI normalisation of the domain, per policy item 1.

### Phase 5 — Close the leak

The backfill is remediation; the crawl intake path still drops publisher metadata. Amend the loose
article intake so `publisherName`, `publisherDomain`, `publisherLocation`, `publisherCountry` and
`rawNewsApiResponse.source` are carried into outlet creation, and add the outlet-schema conformance
check to the pre-cascade gate in [[enforce-pre-cascade-enrichment-gate]]. Without this, the next
crawl batch reopens the same gap.

Only 2,174 of 5,841 current loose inputs carry a non-empty `publisherCountry` (37%), so Phase 5 does
not eliminate the ladder — it just stops rung 1 evidence from being thrown away.

## Gates

1. **Sampled verification before each apply pass.** Draw 25 rows at random from the proposed table
   and verify each against its cited evidence. Abort the pass on more than one incorrect assignment.
2. **Volume-weighted spot-check on T0.** Verify every T0 assignment among the top 50 outlets by
   `articleCount` by hand — this is where the `irishsun.com` failure mode does real damage.
3. **No silent overwrite.** An outlet that already has a country is never rewritten by this plan.
4. **Re-run the country-cut analysis after each phase** and record the delta, so the effect of the
   backfill on published figures is traceable.
5. **Rebuild `index/wiki.db`.** It was last built 2026-07-03 and holds 9,299 articles against 27,155
   on disk, and 395 outlets against 2,080. Any tile or query routed through it today answers from a
   two-month-old vault, and it will not reflect this backfill until rebuilt.

## Decision notes required

- **`backfill-outlet-country-from-saved-publisher-metadata`** — authorises retroactive application of
  [[adopt-ai-metadata-and-event-adjudication-policy]] items 1-4 to outlets created before it, and
  freezes the tier definitions and their differing review postures.
- **`carry-publisher-metadata-into-outlet-creation`** — Phase 5's intake change and the outlet-schema
  conformance gate.

## See also

- `entities/outlet/index.md` — the frozen outlet schema and list convention
- `entities/decisions/adopt-ai-metadata-and-event-adjudication-policy.md` — the ladder this plan applies
- `scripts/people_country_inference_procedure.md` — the propose-then-apply pattern this plan follows
- `scripts/loose_article_ingest_goal_contract.md` — where Phase 5's intake change lands
