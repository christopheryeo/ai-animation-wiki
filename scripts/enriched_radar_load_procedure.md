---
type: procedure
name: enriched-radar-load
status: superseded
last_updated: 2026-08-30
---

# Enriched Radar Input Load Procedure (Historical)

This split-path procedure is retained only to explain historical bundles. It is not an active load
procedure and must not be used for new database loads.

Compiled Markdown is now the sole source of truth. For a new batch:

1. Follow `scripts/loose_article_batch_procedure.md` for route and enrichment.
2. Follow `scripts/entity_cascade_procedure.md` to compile and cascade the articles.
3. Use the UAT delta automatically prepared and verified by `scripts/ingest_cascade.py`.
4. Load only through `scripts/project_wiki_to_uat.py`, after attributed approval bound to the exact
   verified bundle ID.

The retired `stage_enriched_radar_inputs.py` and `stage_mysql_feeds.py` scripts remain available only
to verify old evidence. Production `AI_Animation` remains read-only and is never a projection target.
