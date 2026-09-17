# AI Animation

AI Animation is a Markdown knowledge wiki for monitoring animation, character, merchandising, and
studio-business developments in the United States, Southeast Asia, and Japan.

## Operating model

- Markdown is the canonical source of truth.
- Catalogues, SQLite indexes, dashboards, and database projections are derived and reproducible.
- New source material lands in `Inputs/articles/YYYY-MM/` and is compiled into `entities/article/YYYY-MM/`.
- Entity registries use a hand-maintained `index.md`, a generated `catalog.md`, and an append-only `log.md`.
- No live web enrichment is permitted unless the source URL and retrieval timestamp are recorded.
- Production databases are read-only. UAT scripts require explicit configuration and approval.

## Scope

- Animation production and technology
- Characters, franchises, and intellectual property
- Licensing, merchandising, distribution, and commercial partnerships
- Animation studios and the studio economy
- United States, Southeast Asia, and Japan

The initial taxonomy contains 60 monitoring topics and nine advisory source records. It contains no
articles or customer data. Add future subjects only through the governed procedures in `scripts/`.

## Structure

- `Inputs/articles/` — new source notes awaiting compilation
- `raw/` — optional preserved source payloads
- `entities/` — canonical domain notes
- `topics/canonical-topics.yaml` — canonical 60-topic registry
- `schemas/` — article and outlet schemas
- `scripts/` — adapted operational tools and procedures
- `tests/` — synthetic, content-neutral validation suite
- `dashboards/` — derived dashboard definitions
- `index/` — generated database output
- `runs/` — operational evidence and manifests

## First use

1. Review the canonical Topic Entity notes in `entities/topic/`. Add a future topic through
   `scripts/add_topic.md`; it must follow `entities/topic/_template.md`, including its advisory
   Source Profile.
2. Crawl registered topics through the governed two-source union in
   `scripts/topic_crawl_plan.md`; use `scripts/topic_crawl_goal_contract.md` only for an approved
   autonomous batch.
3. Validate with `scripts/input_article_contract.py` and `scripts/article_quality.py`.
4. Compile and cascade with `scripts/ingest_cascade.py`, then generate the per-topic Crawl Log with
   `scripts/build_topic_crawl_log.py --write`.
5. Rebuild catalogues with `scripts/generate_catalog.py`, then run `scripts/check_links.py` and the
   unit test suite.

Japanese-language source lanes are recorded as planned advisory lanes. They remain inactive until a
provider-language preflight validates the NewsAPI.ai language code and retrieval behaviour.

## Safety

No credentials, database endpoints, hosting configuration, or production deployment are included. Environment-dependent scripts must remain in dry-run or read-only mode until a separately approved configuration is supplied.
