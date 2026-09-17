---
type: procedure
name: list-issues
status: active
last_updated: 2026-08-19
applies_to: [entities/issues]
---

# List Issues Procedure

Use this read-only procedure whenever a person, agent, script, or another procedure needs the
current list of filed Issue entities. It retrieves issue objects already recorded under
`entities/issues/`; it does not run the Issue Radar, create or update an issue, scan article prose,
query production, or write to the search cache.

For general entity and Topic inventories, use `scripts/topic_list.md`. This
procedure is the Issues-specific interface: it defines issue lifecycle scopes, alert eligibility,
ordering, validation, and a stable machine-readable handoff.

> **CALLING INSTRUCTIONS**
>
> - **All filed issues:** “Follow `scripts/issues_list.md`.”
> - **Active watchlist:** “Follow `scripts/issues_list.md`. Scope: `active`.”
> - **Issues to surface:** “Follow `scripts/issues_list.md`. Scope: `alertable`.”
> - **Machine handoff:** “Follow `scripts/issues_list.md`. Scope: `<scope>`. Format:
>   `json`.”

## Request contract

Normalize the caller's request to the following fields. A natural-language caller may omit all of
them; another procedure should pass an explicit JSON-equivalent request.

| Field | Required | Allowed values and default |
|---|---:|---|
| `scope` | no | `all` (default), `active`, `alertable`, or `historical` |
| `statuses` | no | One or more of `hot`, `warm`, `watch`, `closed`, `dismissed` |
| `ramifications` | no | One or more of `severe`, `high`, `moderate`, `low` |
| `minimumScore` | no | Number from `0` through `1`, inclusive |
| `fields` | no | Issue catalog columns; defaults to the eight core fields below |
| `sort` | no | Ordered field/direction pairs; defaults to the canonical Issues order |
| `offset` | no | Non-negative integer; defaults to `0` |
| `limit` | no | Positive integer; omitted means all matching issues |
| `format` | no | `markdown`, `json`, or `ids`; defaults to `markdown` for a person and `json` for another procedure |
| `verified` | no | Boolean; defaults to `true` |

The default core fields are `issueId`, `displayName`, `status`, `ramification`, `score`,
`firstFlagged`, `lastScored`, and `articleCount`. A caller may also request `clusterTags`, `aliases`,
or `File`, provided those columns exist in the current catalog.

Reject unknown fields, status values, ramification values, sort keys, negative offsets,
non-positive limits, scores outside `0`–`1`, or unsupported formats. Do not silently drop a bad
filter.

### Scope definitions

Apply the selected scope first, then intersect it with any explicit `statuses`, `ramifications`, or
`minimumScore` filters:

| Scope | Included issues |
|---|---|
| `all` | Every filed Issue entity, including closed and dismissed calibration records |
| `active` | `hot`, `warm`, and `watch` |
| `alertable` | `hot` or `warm` **and** ramification of `moderate`, `high`, or `severe` |
| `historical` | `closed` and `dismissed` |

`active` is a request macro, not an Issues status value. Never filter the catalog for literal
`status: active`. `alertable` reproduces the delivery threshold in
`scripts/issue_radar_procedure.md`; it does not mean that the issue was newly flagged in the latest
radar run.

## Procedure

### Step 1 — Read the current roster index

Use `entities/issues/catalog.md` as the roster index. The deterministic helper is the preferred
interface:

```bash
python3 scripts/query.py list issues --limit 400
```

The command is read-only and returns the catalog columns and rows as JSON. The Issues domain is
expected to stay below the helper's 400-row cap. If `total` is greater than `returned`, parse the
complete Markdown table in `entities/issues/catalog.md` with a deterministic file/data operation;
do not return a truncated list as complete.

Do not build the roster from radar output. Radar flags are tag-level candidates, while this list is
made of filed issue objects after clustering and judgment. Do not use `index/wiki.db`, a dashboard,
UAT, or production to override the Markdown Issues domain.

### Step 2 — Validate the source before filtering

When `verified` is `true`, perform all of these checks before claiming the result is current and
complete:

1. Run `python3 scripts/validate_issues.py` and require `result: PASS`.
2. Compare the helper's `total` and the catalog frontmatter `note_count` with the current number of
   direct `entities/issues/*.md` entity files, excluding `index.md`, `catalog.md`, `log.md`,
   `_template.md`, and hidden files. All three counts must agree.
3. Require one unique, non-empty `issueId` per row and a matching `<issueId>.md` file.
4. Mechanically compare each row's core fields with that note's frontmatter. This is a bounded
   metadata freshness check, not a prose/content query. The catalog and note values must agree for
   `issueId`, `displayName`, `status`, `ramification`, `score`, `firstFlagged`, `lastScored`, and
   `articleCount`.
5. Confirm that every requested field, filter key, and sort key is present in the catalog header.

If a check fails, do not regenerate the catalog or edit an issue as part of this procedure. Return
`complete: false`, name the mismatch in `warnings`, and use source-note values only to explain the
failure—not to construct a mixed-source roster. Catalog repair is a separate authorized write via
`python3 scripts/generate_catalog.py issues`, followed by `python3 scripts/validate_issues.py`.

If `verified` is explicitly `false`, the caller accepts a catalog snapshot. Set `complete: false`
and include `"Roster was not freshness-verified"` in `warnings`; never describe it as current.

### Step 3 — Filter, sort, and paginate

Apply operations in this exact order:

1. expand and apply `scope`;
2. intersect explicit `statuses`;
3. intersect explicit `ramifications`;
4. apply `score >= minimumScore` when supplied;
5. sort;
6. calculate `totalMatching`;
7. apply `offset`, then `limit`.

Unless the caller supplies another sort, use the Issues domain's canonical order:

1. `status`: `hot`, `warm`, `watch`, `closed`, `dismissed`;
2. `ramification`: `severe`, `high`, `moderate`, `low`;
3. `score` descending;
4. `displayName` A–Z;
5. `issueId` A–Z as the stable final tie-breaker.

Sort `score` numerically, not lexically. Filtering is case-insensitive, but return canonical
lowercase status and ramification values.

### Step 4 — Render the requested format

For `json`, serialize and parse the result once before handoff. Return no prose outside this stable
envelope when another procedure is the caller:

```json
{
  "schemaVersion": "issue-list.v1",
  "scope": "all",
  "filters": {
    "statuses": [],
    "ramifications": [],
    "minimumScore": null
  },
  "sort": [
    "status:canonical",
    "ramification:canonical",
    "score:desc",
    "displayName:asc",
    "issueId:asc"
  ],
  "offset": 0,
  "limit": null,
  "totalMatching": 0,
  "returned": 0,
  "complete": true,
  "source": "entities/issues/catalog.md",
  "catalogGeneratedAt": "2026-08-19T00:00:00",
  "generatedAt": "2026-08-19T00:00:00Z",
  "warnings": [],
  "issues": []
}
```

`catalogGeneratedAt` comes from the catalog frontmatter. `generatedAt` is the current UTC response
time. `totalMatching` is calculated after filtering and before pagination. `complete` can be `true`
only when `verified` is true, Step 2 passes, and all matching rows are returned after the requested
offset/limit; a deliberately paginated result therefore has `complete: false` without implying a
source-validation failure.

For `ids`, return one `issueId` per line in the selected order, without bullets or commentary. A
calling procedure that needs completeness metadata must request `json`, not `ids`.

For `markdown`, follow `entities/issues/index.md` under `## Producing a List`: group by status, then
ramification, and sort by score descending then display name. To supply the required 15-word
Assessment summary, read only the `## Assessment` section of each returned note after the roster is
validated. Do not use that prose to alter identity, status, ramification, or score.

### Step 5 — Handoff

Another procedure must request `format: json`, record the exact request it made, consume only the
returned `issues` array, and inspect `complete` and `warnings` before proceeding. It must not widen
the scope or infer omitted issues after the handoff.

If `complete` is false because of validation drift, a caller that requires a full current roster
must stop. A caller that explicitly supports partial results may continue only after recording the
warning and the returned IDs.

## Examples

All issue IDs for another procedure:

```json
{
  "scope": "all",
  "fields": ["issueId"],
  "format": "json"
}
```

Current active watchlist for a person:

```json
{
  "scope": "active",
  "format": "markdown"
}
```

Alert-layer handoff for the Issue Radar procedure:

```json
{
  "scope": "alertable",
  "fields": [
    "issueId",
    "displayName",
    "status",
    "ramification",
    "score",
    "firstFlagged",
    "lastScored",
    "articleCount"
  ],
  "format": "json",
  "verified": true
}
```
