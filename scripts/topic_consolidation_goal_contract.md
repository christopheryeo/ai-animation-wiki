---
type: goal-contract
name: ai-topic-consolidation
status: complete
owner: Christopher (chris@sentient.io)
created: 2026-08-12
applies_to: [entities/topic, entities/article, archive/topic-legacy, UAT projection]
---

# AI Topic Consolidation Goal Contract

This contract executes [[consolidate-topics-to-ai-controlled-taxonomy]]. It does not change the
future intake or cascade process.

## 1. Objectives

1. Reduce active topic notes from the fresh execution baseline to 60–80 canonical topics.
2. Assign every compiled article exactly one primary and at most two secondary canonical topics.
3. Preserve every original article field and topic value while updating the active topic links and
   database projection's `article.topic` to the primary canonical label.
4. Preserve retired topic notes in a verified archive; delete no source evidence.
5. Finish with exact reciprocal links and counts, clean schemas and links, a zero-change rerun, and
   a verified but unloaded UAT delta.
6. Report the starting and ending active-topic counts, archived count, and reduction percentage.

## 2. Steps

1. Freeze fresh article/topic inventories and SHA-256 hashes; create and verify recoverable archives
   before article or topic mutation.
2. Have AI produce and independently review a 60–80-topic taxonomy with IDs, names, categories,
   definitions, aliases, and evidence-backed boundaries.
3. Produce complete legacy-topic and per-article assignment manifests. Use article projection topic,
   current topic links, Issue Tags, title, Summary, and Key Points only; do not use live web data.
4. Prepare and hash a deterministic migration bundle. Require complete mappings, unique canonical
   IDs, one primary per article, at most two secondary assignments, and no fallback rate above 1%.
5. Apply article changes in restartable batches of at most 1,000. Change only operational
   `last_updated`, topic links in `## Related Entities`, the new `## Topic Consolidation Audit`, and
   projection `article.topic`; preserve every other value byte-for-byte or structurally exactly.
6. Rebuild canonical topic notes and Coverage from assignments; archive retired notes unchanged;
   rewrite explicit legacy topic links; append required audit-log entries; regenerate catalogs.
7. Run article-quality, link, exact backlink/count, projection-only-diff, archive-resolution,
   completeness, and idempotence gates. Prepare and verify—but do not load—the UAT delta.
8. Write Markdown and JSON reports with counts, timings, hashes, mappings, gates, and throughput.

## 3. Success End Conditions

1. Active topic count and catalog count are equal and between 60 and 80.
2. Every starting topic is retained or archived, and every starting article/source ID remains.
3. Every article has one primary and no more than two secondary canonical topics.
4. Original topic values and links are recoverable from the article audit and hashed bundle.
5. Article source content, provenance, Issue Tags, coverage, media, tags, user groups, and all
   non-topic projection fields are unchanged.
6. Topic Coverage and `articleCount` exactly equal reverse article assignments with no duplicates.
7. No active note links to retired topic paths; historical links resolve to active or archived notes.
8. Article-quality and full-vault link checks have zero hard failures.
9. UAT delta has zero inserts/deletes/child changes and no parent change except `topic`; it is not
   loaded.
10. A second migration preview proposes zero changes.
11. Final reports prominently state the fresh starting and ending active-topic counts.

## 4. Breakout Conditions

Stop and report exact records, artifacts, and elapsed time if: a frozen input changes; backup or
hash verification fails; a note cannot parse; 60–80 coherent topics cannot cover the corpus without
material loss; an article lacks a primary, exceeds three assignments, or fallback exceeds 1%; an
independent review remains contradictory; a slug/alias/archive collision is not deterministic; a
preview changes unauthorized article content; any identity, metadata, provenance, or projection
child value would be lost; a batch cannot resume safely; counts/backlinks disagree; a quality,
link, projection, catalog, or idempotence gate fails; UAT diff includes anything beyond topic-only
updates; a command targets production, loads a database, edits `raw/`, or changes future intake; or
the goal execution bound is reached.

## 5. Goal Invocation

> Execute `scripts/topic_consolidation_goal_contract.md` completely. Continue until every success
> end condition passes or a breakout condition requires my decision.
