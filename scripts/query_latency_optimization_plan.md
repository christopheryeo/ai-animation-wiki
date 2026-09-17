# Query Latency Optimization Plan

## Objective

Modify `scripts/query.py` so the existing cache-off benchmark falls from
**17.09 seconds to no more than 8.55 seconds per query** using the current
GPT-5.6 model.

`scripts/bench_query.py` remains unchanged and serves only as the measurement
harness. Each benchmark iteration runs exactly one golden question.

## `query.py` Changes

1. Preserve the existing `run_query` signature, CLI commands, HTTP endpoint,
   result fields, cache semantics, and fallback behavior.
2. Add an internal deterministic context-building stage that:
   - resolves exact entity names, aliases, and acronyms locally;
   - classifies identity, relationship, coverage, roster, appointment, and
     existence-check questions;
   - extracts only relevant note sections instead of returning entire notes;
   - computes shared coverage, related entities, and article rankings locally;
   - enforces bounded context sizes.
3. Send supported questions through one structured GPT-5.6 request instead of
   the current repeated tool-call loop.
4. Use `reasoning.effort: low` for the one-call path and retain medium effort
   for the existing fallback path.
5. Answer purely deterministic roster and appointment queries locally when no
   synthesis is required.
6. Replace the full repeated procedure text with a compact runtime prompt that
   preserves all grounding, citation, sensitivity, and answer-delivery rules.
7. Fall back automatically to the existing agentic tool loop whenever
   resolution is missing, ambiguous, unsupported, or insufficiently grounded.
8. Keep cache persistence after answer generation exactly as it works today.
9. Add an environment rollback switch, `QUERY_FAST_PATH=false`, that restores
   the existing execution path immediately.

## Validation

1. Leave `scripts/bench_query.py` unchanged.
2. Add focused `query.py` tests for:
   - entity and alias resolution;
   - query classification;
   - minimal section extraction;
   - roster and appointment rendering;
   - single-call routing and fallback selection;
   - cache-read and cache-write compatibility.
3. Run `python3 scripts/bench_query.py --runs 8` to verify every golden case
   still satisfies its expected facts, entities, sources, and traversal
   constraints.
4. Run the unchanged default benchmark three times in separate processes with
   cache reading and writing off. This is 15 API queries in total: five
   one-question iterations per benchmark execution.

## End Condition

The work is complete only when:

1. Three consecutive unchanged benchmark executions average **no more than
   8.55 seconds per query**.
2. All eight golden cases pass without fabricated facts, missing entities,
   unrelated sources, or cache writes.
3. Supported fast-path queries use no more than one API request.
4. Ambiguous queries continue to work through the legacy fallback.
5. Setting `QUERY_FAST_PATH=false` restores the current behavior.

## Breakout Conditions

Stop and reassess if any of these conditions occurs:

1. The single-call GPT-5.6 path itself averages above **8.55 seconds**; further
   orchestration changes cannot overcome that model latency floor.
2. Two successive `query.py` optimization rounds each improve latency by less
   than **10%** while the average remains above target.
3. Reaching the target requires weakening golden-answer quality or
   source-grounding requirements.
4. The effort exceeds **60 evaluation API calls or two engineering days**
   without meeting the target.

At breakout, report the measured limiting factor and propose revisiting one
fixed constraint: model choice, cache-off measurement, or the 50% target.

## Assumptions

- Performance work is confined to `scripts/query.py`; `bench_query.py` is not
  modified.
- GPT-5.6/Sol remains the model.
- The cache-off benchmark remains the performance baseline.
- Answer quality and provenance cannot regress.
- Before implementation changes the query operating rule, record the accepted
  architecture in `entities/decisions/` as required by the project governance
  procedure.
