---
type: procedure
name: topic-list
status: active
last_updated: 2026-08-19
applies_to: [entities, entities/topic, topics]
---

# Entity and Topic List Query Procedure

Use this read-only procedure when a user, agent, script, or another procedure needs to discover:

1. which entity domains exist;
2. the members of one or more entity domains; or
3. the canonical Topic entities and their current metadata.

This is an inventory operation, not a content query. It returns current roster data without opening
every entity note, scanning article prose, changing the search cache, or writing any vault file.
For a natural-language question about what an entity did, said, or is connected to, use
`scripts/query_procedure.md` instead.

When the requested domain is `issues`, follow `scripts/issues_list.md` as the
Issues-specific wrapper. It adds lifecycle scopes, alert eligibility, source-note freshness checks,
and the stable `issue-list.v1` handoff contract while retaining this procedure's catalog-first
inventory rules.

## Terms and sources of truth

- An **entity domain** is a direct child of `entities/` with an `index.md` and entity notes. Examples
  include `people`, `organisations`, `outlet`, `country`, `place`, `appointments`, `topic`, `tag`, and
  `issues`.
- A **canonical topic** is an entity note under `entities/topic/`. The domain name is singular:
  `topic`, not `topics`.
- `entities/<domain>/catalog.md` is the generated query index for a domain. Use it for roster reads,
  but remember that the Markdown entity notes remain the source of truth.
- `topics/canonical-topics.yaml` is the machine-readable canonical topic-definition mirror used by
  topic assignment and crawl orchestration. `topics/sa26.json` and any other files under `topics/`
  are monitoring configurations, not additional Topic entities.
- `index/wiki.db` and dashboard payloads are derived artifacts. Do not use them to override a current
  Markdown note or catalog.

## Request contract

Before reading data, normalize the caller's request to these fields:

| Field | Required | Meaning |
|---|---:|---|
| `operation` | yes | `list-domains`, `list-members`, or `list-topics` |
| `domains` | for `list-members` | One or more exact singular folder names under `entities/` |
| `filters` | no | Exact field/value tests against catalog columns |
| `fields` | no | Columns to return; defaults to all catalog columns |
| `sort` | no | Ordered catalog fields plus `asc` or `desc` |
| `offset` | no | Zero-based starting row; defaults to `0` |
| `limit` | no | Positive maximum rows; omitted means all matching rows |
| `format` | no | `markdown`, `json`, or `ids`; defaults to `markdown` for a person and `json` for a procedure |

Reject an unknown operation, domain, field, filter column, sort column, negative offset, or
non-positive limit. Never silently reinterpret `topics` as an entity domain: explain the distinction
and use `topic` only when the caller confirms that canonical Topic entities are wanted. If the caller
simply asks for "the topic list", canonical Topic entities are the default.

Filters are exact, case-insensitive string matches unless the caller explicitly requests another
operator. Do not implement fuzzy matching as a roster filter. Empty values, `null`, and missing
columns are distinct states and must not be collapsed without an explicit request.

## Procedure

### Step 1 — Establish the current domain inventory

For `list-domains`, enumerate only direct subdirectories of `entities/`. Exclude hidden entries and
do not infer domains from `wiki.yaml`, because that manifest's registered-core list may be narrower
than the live entity surface.

A domain is roster-queryable when `entities/<domain>/catalog.md` exists. Report any direct entity
folder that lacks a catalog as `queryable: false`; do not omit it. Do not count `index.md`,
`catalog.md`, `log.md`, `_template.md`, hidden files, or nested article month folders as entities.

Return, at minimum, the exact domain name and whether its catalog exists. Include the catalog's
declared `note_count` when present. If the caller asks for a verified count, perform the completeness
check in Step 5 before returning it.

### Step 2 — Resolve canonical topic scope

For `list-topics`, use `entities/topic/catalog.md`. The standard topic fields are:

`topicId`, `displayName`, `category`, `aliases`, `articleCount`, `lastCrawledAt`, and `File`.

Use `topics/canonical-topics.yaml` only when the caller explicitly requests topic definitions,
keywords, assignment limits, or the canonical configuration mirror. Use other files under `topics/`
only when the caller explicitly asks for monitoring definitions. Never merge rows from these
surfaces into a larger apparent topic roster.

### Step 3 — Read the roster index

For a single, small domain, the deterministic helper is the preferred interface:

```bash
python3 scripts/query.py list topic --limit 400
python3 scripts/query.py list people --limit 100
```

It returns JSON with `domain`, `total`, `returned`, `columns`, and `rows`. It does not call a model or
write the query cache.

The helper has a hard 400-row return cap. If `total > returned`, or if a complete roster from any
potentially large domain is requested, parse the complete Markdown table in
`entities/<domain>/catalog.md` with a deterministic file/data operation. Do not make repeated helper
calls and pretend they are pages: the current helper has no `offset` parameter. Apply requested
filters, sort, offset, limit, and field projection only after the complete catalog table has been
parsed.

Never obtain a roster by opening every note, grepping article files, reading `Inputs/`, or querying
production `AI_Animation`.

### Step 4 — Order and render

For a complete human-readable roster, read and follow the target domain's `## Producing a List`
section in `entities/<domain>/index.md`. That section controls grouping, nesting, labels, and sort
order. For topics, group by `category` A-Z, place blank categories in a trailing
`(Uncategorised)` group, and sort within each group by `articleCount` descending then `displayName`
A-Z.

For explicit caller-provided sorting, use the caller's order instead. Add `displayName` A-Z and then
the stable entity ID as deterministic tie-breakers unless the caller supplied other tie-breakers.

Output formats:

- `markdown`: follow the domain list convention; for topics use
  `- [[<topicId>|<displayName>]]` unless plain text was requested.
- `ids`: one stable entity ID per line, with no bullets or commentary.
- `json`: serialize and parse once before delivery. Use this stable envelope:

```json
{
  "operation": "list-topics",
  "domains": ["topic"],
  "filters": {},
  "sort": ["category:asc", "articleCount:desc", "displayName:asc"],
  "offset": 0,
  "limit": null,
  "totalMatching": 0,
  "returned": 0,
  "complete": true,
  "generatedAt": "2026-08-19T00:00:00Z",
  "items": []
}
```

`totalMatching` is the count after filters and before pagination. `returned` is the number of items
in this response. `complete` is true only when all matching rows are returned and Step 5 passes.
`generatedAt` is the current UTC response time, not the catalog generation time. Preserve catalog
values as strings unless the domain schema clearly defines a number, boolean, list, or null.

### Step 5 — Validate completeness and freshness

Before returning the result:

1. Confirm the catalog header columns exactly match every requested field, filter, and sort key.
2. Compare the parsed table-row count with the catalog frontmatter `note_count`, when present.
3. For a verified complete roster, enumerate current Markdown files directly. Exclude system files,
   hidden files, `log-*.md` ledgers, and any domain-specific archive or nested-navigation files
   described by its `index.md`. Count a remaining file as an entity note only when it has the
   domain's required stable-ID field, and require its filename to equal `<stable-id>.md`. Report
   malformed names, duplicate stable IDs, and stray Markdown artifacts separately. The catalog
   count, parsed-row count, and conforming direct-note count must agree.
4. For canonical topics, also compare the set of `topicId` values in the active catalog with the set
   in `topics/canonical-topics.yaml`. Report missing or extra IDs; do not repair either surface as
   part of this read-only procedure.
5. Confirm every returned item has a non-empty stable ID and that IDs are unique in the response.
6. Confirm `returned <= limit` when a limit is present, and that offset/limit slicing happened only
   after filtering and sorting.
7. If any required check fails, return `complete: false` with a concise `warnings` array. Do not
   claim that the roster is complete or current.

A catalog does not need regeneration merely because it was generated earlier. Regenerate only when
the checks prove drift and the caller separately authorizes a write operation. This procedure itself
is always read-only.

## Handoff rules for other procedures

When called as a subprocedure, return JSON by default and include no narrative outside the JSON
object. The calling procedure must record the exact request parameters it used and must not widen
the scope after receiving the result. In particular:

- an Issues-roster caller must use `scripts/issues_list.md` and inspect its `complete` and
  `warnings` fields before proceeding;
- a topic-crawl caller should normally request `topicId`, `displayName`, `lastCrawledAt`, and any
  selection fields it actually needs;
- an entity-resolution caller should use `scripts/query.py resolve`, not scan a full roster for a
  substring match;
- a natural-language answer caller remains responsible for the cache and delivery rules in
  `scripts/query_procedure.md`;
- a caller must stop or explicitly disclose incompleteness when `complete` is false.

## Examples

Canonical topics with crawl status, for another procedure:

```json
{
  "operation": "list-topics",
  "fields": ["topicId", "displayName", "category", "lastCrawledAt"],
  "sort": ["category:asc", "displayName:asc"],
  "format": "json"
}
```

Watch-status issue entities for a human-readable roster:

```json
{
  "operation": "list-members",
  "domains": ["issues"],
  "filters": {"status": "watch"},
  "format": "markdown"
}
```

Stable IDs for all people, with deterministic pagination:

```json
{
  "operation": "list-members",
  "domains": ["people"],
  "fields": ["personId"],
  "sort": ["displayName:asc"],
  "offset": 0,
  "limit": 200,
  "format": "ids"
}
```
