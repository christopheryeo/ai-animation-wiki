---
type: procedure
name: end-topic-crawl
status: active
last_updated: 2026-08-14
---

# End Topic Crawl Procedure

Use this procedure to record that one canonical topic has completed a crawl. Calling the procedure
is the instruction and authorization to perform the update immediately. Do not request a crawl
receipt, preview the change, or ask for a second confirmation.

This procedure updates crawl state only. It does not call the crawl-query endpoint, ingest returned
articles, or change the topic's Crawl Prompt. A successful update sets `lastCrawledAt` and
`crawlStatusAt` to the normalized completion time and sets `crawlStatus` to `Completed`.

The calling environment performs the entire checkpoint update directly from this procedure. The
runtime path must not hand work to Python, a shell command, or a dedicated checkpoint-updater
program.

> **USER OR CALLER INSTRUCTIONS — use one of these forms:**
>
> - **Current time:** “Follow `scripts/end_topic_crawl.md` for `<topicId or exact display name>`.”
> - **Endpoint completion time:** “Follow `scripts/end_topic_crawl.md` for
>   `<topicId or exact display name>`. The crawl completed at
>   `<timezone-qualified RFC 3339 timestamp>`.”

## Inputs

1. One exact topic name, `topicId`, or alias.
2. Optional timezone-qualified completion time. If omitted, use the current time.

## Procedure

1. **Capture the checkpoint input.** Record the supplied topic value and completion timestamp. If no
   timestamp is supplied, capture the current timezone-aware instant. Do not infer a timestamp from
   an article date, file modification time, endpoint start time, or other indirect evidence.

2. **Resolve exactly one Topic Entity.** Read the active Topic records and match the supplied value
   case-insensitively against `topicId`, `displayName`, filename, and aliases. Never use partial or
   fuzzy matching. Stop without changing files if there is no match or more than one match. This
   procedure never creates a topic.

3. **Validate and normalize the completion time.** Require a timezone-qualified RFC 3339 timestamp,
   convert it to UTC, and serialize it as `YYYY-MM-DDTHH:MM:SSZ`. Reject it without mutation if it is
   invalid, more than five minutes in the future, or not later than the topic's existing non-null
   `lastCrawledAt`. If the supplied value contains fractional seconds, retain its actual instant for
   comparison but store the canonical value at whole-second precision.

4. **Take a rollback snapshot.** Before editing, preserve the complete current contents of the
   resolved Topic Entity note, `entities/topic/log.md`, and `entities/topic/catalog.md` when it
   exists. These snapshots are temporary recovery state, not crawl receipts or approval previews.

5. **Update the Topic Entity directly.** Replace exactly one frontmatter `lastCrawledAt` field with
   the normalized UTC value, replace exactly one `crawlStatus` field with `Completed`, and replace
   exactly one `crawlStatusAt` field with the same normalized UTC value. Treat the three writes as
   one atomic state transition. Do not change any other frontmatter field, heading, Crawl Prompt,
   Coverage entry, definition, note, or formatting. If the note does not contain exactly one of
   each required field, restore the snapshot and stop.

6. **Append the audit entry directly.** Append exactly one new line to `entities/topic/log.md` using
   the current timezone-aware audit timestamp, canonical topic wikilink, normalized checkpoint, and
   the reason that the completed crawl and `Completed` status were recorded through
   `end_topic_crawl.md`. Never rewrite or remove an older log entry.

7. **Reconstruct the complete derived Topic catalog using native operations.** Do not patch one
   catalog row. Enumerate every Markdown file directly under `entities/topic/`, excluding
   `index.md`, `catalog.md`, `log.md`, and `_template.md`; parse each note's frontmatter; and render
   a fresh complete catalog using the Topic registry field order `topicId`, `displayName`,
   `category`, `aliases`, `articleCount`, `lastCrawledAt`, `crawlStatus`, `crawlStatusAt`, followed
   by `File`. Sort records by
   `displayName` case-insensitively, preserve the standard generated header and Markdown-table
   shape, set `note_count` and the displayed count from the enumerated records, use the current
   timezone-aware generation time, and atomically replace `entities/topic/catalog.md`. This is a
   full generated-artifact rebuild performed through the caller's native file and data operations,
   not a manual catalog edit.

8. **Validate the complete checkpoint state using native operations.** Re-enumerate every active
   Topic Entity and verify all of the following before committing the change:

   - every active topic has exactly one textual `lastCrawledAt`, `crawlStatus`, and `crawlStatusAt` field;
   - every checkpoint is either `null`/blank or a timezone-qualified timestamp;
   - every status is one of `Not started`, `Queued`, `In progress`, `Completed`, `Failed`, or `Cancelled`;
   - `crawlStatusAt` is null only for `Not started` and is otherwise a timezone-qualified timestamp;
   - the resolved note contains the normalized UTC checkpoint and status time exactly once and
     contains `crawlStatus: Completed` exactly once;
   - the catalog record count equals the active Topic Entity count;
   - the catalog contains exactly one row for every `topicId`, including exactly one target row
     whose checkpoint matches the resolved note;
   - exactly one new matching audit entry was appended; and
   - the changed note, log, and rebuilt catalog remain readable Markdown with valid frontmatter and
     no newly broken wikilinks.

9. **Rollback on any failure.** If the note update, log append, catalog regeneration, or validation
   fails, restore all snapshotted files. Report a checkpoint failure and do not claim success.

10. **Report completion.** Only after every gate passes, report the topic display name, its new UTC
    `lastCrawledAt` value, and `crawlStatus: Completed`. No receipt, preview, or second confirmation
    is required.

## Runtime requirement

This procedure is runtime-neutral. An AI agent may use its native file tools, and an n8n workflow
may use file/storage nodes plus a JavaScript Code node or equivalent native data operations. The
implementation must provide complete catalog reconstruction, validation, atomic replacement, and
snapshot restoration as specified above. It must not invoke Python, shell commands, or a separate
checkpoint program anywhere in the topic-crawl completion path.

Existing vault maintenance utilities may still be used independently for scheduled maintenance or
engineering checks, but `start_topic_crawl.md` and this procedure do not call them.

## Important rule

Only call this procedure after a crawl has completed. `start_topic_crawl.md` calls it only after a
valid endpoint response with `status: completed`. The procedure treats the call itself as
confirmation and requires no separate evidence or approval.
