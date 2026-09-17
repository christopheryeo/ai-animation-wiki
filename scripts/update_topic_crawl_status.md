---
type: procedure
name: update-topic-crawl-status
status: active
last_updated: 2026-08-19
---

# Update Topic Crawl Status Procedure

Use this procedure to change the current crawl workflow status of one canonical Topic Entity. Calling
the procedure with a topic and target status authorizes the update immediately; do not ask for a
preview or second confirmation.

This procedure changes `crawlStatus` and `crawlStatusAt` only. It never calls the crawl endpoint,
changes the Crawl Prompt, ingests articles, or clears `lastCrawledAt`. A transition to `Completed`
is closed through `scripts/end_topic_crawl.md`, because only that procedure may advance the latest
successful crawl checkpoint.

> **USER OR CALLER INSTRUCTIONS — use this form:**
>
> “Follow `scripts/update_topic_crawl_status.md`. Topic: `<topicId or exact display name>`.
> Status: `<Not started | Queued | In progress | Completed | Failed | Cancelled>`.
> Reason: `<brief operational reason>`.”
>
> Optionally add: “The status changed at `<timezone-qualified RFC 3339 timestamp>`.”
>
> For `Completed`, provide the actual crawl completion time instead: “The crawl completed at
> `<timezone-qualified RFC 3339 timestamp>`.”

## Inputs

1. One exact topic name, `topicId`, filename, or alias.
2. One target status: `Not started`, `Queued`, `In progress`, `Completed`, `Failed`, or `Cancelled`.
3. Optional timezone-qualified transition time. If omitted, use the current time.
4. Optional concise reason. If omitted, record `explicit operator status update`.
5. For `Completed`, an actual completion time accepted by `scripts/end_topic_crawl.md`.

Status matching is case-insensitive after trimming whitespace, but the stored value must use the
canonical capitalization shown above. Do not accept synonyms such as `ready`, `running`, `done`, or
`error`; the caller must choose one canonical status.

## Allowed transitions

| Current status | Permitted target status |
| --- | --- |
| `Not started` | `Queued` |
| `Queued` | `In progress`, `Failed`, `Cancelled` |
| `In progress` | `Completed`, `Failed`, `Cancelled` |
| `Completed` | `Queued` |
| `Failed` | `Queued` |
| `Cancelled` | `Queued` |

An update to the status already stored is an idempotent no-op: make no file or log change and report
that the topic was already in the requested state. Reject every transition not listed above. In
particular, do not reset a previously crawled topic to `Not started`, skip directly from
`Not started` to `In progress`, or mark a topic `Completed` unless it is currently `In progress`.

## Procedure

1. **Capture the request.** Record the supplied topic, target status, optional transition time, and
   reason. Normalize the target to its canonical capitalization. Reject a blank topic, unsupported
   status, or blank explicitly supplied reason before changing files.

2. **Resolve exactly one canonical Topic Entity.** Enumerate the active Topic notes directly under
   `entities/topic/`. Exclude `index.md`, `catalog.md`, `log.md`, `_template.md`, files beginning
   `log-`, and Dropbox conflicted-copy files. Require each active filename to equal
   `<frontmatter topicId>.md`. Match the supplied value case-insensitively against exact `topicId`,
   `displayName`, filename, and aliases. Never use partial or fuzzy matching. Stop without mutation
   if there is no match or more than one match. This procedure never creates a topic.

3. **Validate current state and transition.** Require exactly one `crawlStatus`, `crawlStatusAt`, and
   `lastCrawledAt` field in the resolved note. Require the current status to be canonical and the
   requested transition to appear in the table above. If the target equals the current status,
   report the idempotent no-op and stop.

4. **Route successful completion through the checkpoint procedure.** If the target is `Completed`,
   require the current status to be `In progress` and immediately follow
   `scripts/end_topic_crawl.md` for the resolved canonical topic using the supplied actual crawl
   completion time. That procedure atomically updates `lastCrawledAt`, `crawlStatus`, and
   `crawlStatusAt`, logs the completion, rebuilds the catalog, validates the result, and rolls back
   on failure. Do not perform a second status write or log entry here. Report the result returned by
   the end procedure and stop.

5. **Validate and normalize the transition time.** For every non-completion transition, use the
   supplied timezone-qualified RFC 3339 timestamp or capture the current timezone-aware instant.
   Reject an invalid or timezone-naive value, a value more than five minutes in the future, or a
   value not later than the existing non-null `crawlStatusAt`. Convert the instant to UTC and store
   it as `YYYY-MM-DDTHH:MM:SSZ`. Fractional seconds may be used for comparison but are removed from
   the stored value.

6. **Take a rollback snapshot.** Preserve the complete current contents of the resolved Topic note,
   `entities/topic/log.md`, and `entities/topic/catalog.md` when it exists. These copies are temporary
   recovery state, not a preview or receipt.

7. **Update the Topic Entity atomically.** Replace exactly one `crawlStatus` value with the canonical
   target and exactly one `crawlStatusAt` value with the normalized UTC transition time. Do not
   change `lastCrawledAt` or any other frontmatter field, heading, Crawl Prompt, Coverage entry,
   definition, note, or formatting.

8. **Append exactly one audit entry.** Append one line to `entities/topic/log.md` containing the
   current timezone-aware audit timestamp, canonical topic wikilink, previous and new statuses,
   normalized `crawlStatusAt`, supplied/default reason, and
   `source: scripts/update_topic_crawl_status.md`. Never rewrite or remove earlier entries.

9. **Reconstruct the complete Topic catalog.** Enumerate the same active canonical Topic set from
   Step 2 and rebuild `entities/topic/catalog.md` in full. Use registry field order `topicId`,
   `displayName`, `category`, `aliases`, `articleCount`, `lastCrawledAt`, `crawlStatus`,
   `crawlStatusAt`, followed by `File`; sort by `displayName` case-insensitively; preserve the
   generated header/table shape; set both displayed counts from the enumerated records; and atomically
   replace the catalog. Never patch one row or hand-edit the generated catalog.

10. **Validate the complete state.** Re-enumerate every active canonical Topic Entity and verify:

    - every topic has exactly one `lastCrawledAt`, `crawlStatus`, and `crawlStatusAt` field;
    - every status is one of the six canonical values;
    - `Not started` has null `lastCrawledAt` and null `crawlStatusAt`;
    - every other status has a timezone-qualified `crawlStatusAt`;
    - `Completed` has a non-null timezone-qualified `lastCrawledAt`;
    - the target note contains the requested status and normalized status time exactly once;
    - the target note's `lastCrawledAt` and all non-status content are byte-for-byte unchanged;
    - catalog count and topic IDs exactly equal the active canonical note set;
    - the catalog contains exactly one target row matching the updated note;
    - exactly one matching audit entry was appended; and
    - the changed note, log, and catalog remain readable Markdown with valid YAML and no newly broken
      wikilinks.

11. **Rollback on any failure.** Restore every snapshotted file if the note update, log append,
    catalog reconstruction, or validation fails. Report the failed gate and do not claim that the
    status changed.

12. **Report completion.** Report the canonical display name, previous status, new status,
    normalized UTC `crawlStatusAt`, and reason only after all gates pass.

## Runtime requirement

The procedure is runtime-neutral. An AI agent may use native file tools, and an n8n workflow may use
file/storage nodes plus a JavaScript Code node or equivalent native data operations. The caller must
provide atomic replacement, full catalog reconstruction, validation, and rollback. The procedure
does not require or invoke a dedicated Python or shell program.

## Boundaries

- Use `scripts/start_topic_crawl.md` for the normal automated sequence from selection through the
  endpoint attempt.
- Use `scripts/end_topic_crawl.md` for a directly confirmed successful completion.
- Use this procedure for explicit operational transitions, including manual queueing, failure
  recording, cancellation, and retry queueing.
- This procedure never changes article data, UAT, production, topic taxonomy, Crawl Prompts, or
  `lastCrawledAt` except indirectly through the required `Completed` handoff to
  `scripts/end_topic_crawl.md`.
