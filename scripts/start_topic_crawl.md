---
type: procedure
name: start-topic-crawl
status: superseded
last_updated: 2026-09-17
---

# Start Topic Crawl Procedure

> **Superseded.** Do not use this single-endpoint procedure for a new AI Animation crawl.
> Follow `scripts/topic_crawl_plan.md` instead. It is the governed, restartable two-source
> SET A/SET B method, with relevance gates, retained evidence, cascade validation, and exact
> checkpoint closure. This file remains only as historical starter provenance.

Use this procedure to select one or more Topic Entities and call the configured crawl-query endpoint
once per selected topic. Calling this procedure authorizes the endpoint calls and the Topic Entity
changes expressly required below. It does not authorize article compilation, cascade, database
projection, or production writes outside the crawl endpoint's own agreed responsibility.

Endpoint connection values are supplied by the invoking runtime, such as the operating n8n
workflow, and are deliberately not stored in this wiki. Do not treat the absence of an endpoint URL
or secret in the repository or `.env.local` as proof that the integration is unconfigured. For every
live call, Step 4 must instead validate the runtime binding and report only safe readiness booleans.
Stop only when that invocation's runtime readiness check fails. The no-selector response in Step 2
remains executable without any endpoint check.

> **USER CALLING INSTRUCTIONS — copy and send one of these comments to the agent:**
>
> - **One topic:** “Follow `scripts/start_topic_crawl.md`. Topic: `<topicId or exact display
>   name>`. Article date range: `<YYYY-MM-DD>` to `<YYYY-MM-DD>`.”
> - **Several eligible topics:** “Follow `scripts/start_topic_crawl.md`. Number of topics:
>   `<positive integer>`. Article date range: `<YYYY-MM-DD>` to `<YYYY-MM-DD>`.”
> - **No crawl:** “Follow `scripts/start_topic_crawl.md`.”

## Colleague guide: what this procedure does

This file is an **agent operating procedure**, not an executable crawler and not an API endpoint.
When a caller asks an agent to follow it, the agent performs the orchestration around the crawler:
it validates the caller's parameters, determines which Topic Entity or Entities should be crawled,
ensures that each topic has a usable Crawl Prompt, sends one request per topic to the separately
configured crawl-query endpoint, validates each response, and records successful completion through
`scripts/end_topic_crawl.md`.

The responsibilities are deliberately separated:

| Component | Responsibility |
| --- | --- |
| Caller | Supplies a topic selector or topic count and, when crawling, the article publication-date range. |
| `start_topic_crawl.md` agent | Validates parameters, resolves topics or routes missing ones through `add_topic.md`, ensures Crawl Prompts, calls the endpoint once per topic, validates responses, and summarizes the run. |
| Crawl-query endpoint | Queries NewsAPI.ai or the agreed provider and stores the returned raw article records. |
| `end_topic_crawl.md` caller | Advances a successfully crawled Topic Entity's checkpoint and sets its crawl status to `Completed` using native file/data operations, with no Python or shell handoff. |
| Article ingest/cascade workflow | Later compiles stored raw articles into the wiki and creates their entity links. It is not part of this procedure. |

In plain terms, the flow is:

1. The caller identifies **what to crawl** using either `topic` or `numberOfTopics`.
2. The caller identifies **which publication dates to search** using `articleDateRange`.
3. The procedure selects canonical Topic Entities and obtains each stored Crawl Prompt.
4. Selected topics move through `Queued` and `In progress` as the procedure works.
5. The procedure calls the endpoint separately for each selected topic.
6. A valid completed response advances that topic's crawl checkpoint and sets `Completed`; a
   failure sets `Failed` without erasing the last successful checkpoint.
7. The procedure returns one summary covering the entire requested run.

The procedure does not ask the endpoint to decide the topic taxonomy. Topic identity and the Crawl
Prompt come from the wiki. The endpoint receives those values and is responsible only for executing
the article query and storing its results.

## Parameter reference and interpretation

The parameters are conceptual procedure inputs. A person may state them in a natural-language call,
and an application may represent the same values as JSON. Parameter names are case-sensitive in JSON
examples, but topic matching itself is case-insensitive as described below.

| Parameter | Type | Required? | Meaning | Example |
| --- | --- | --- | --- | --- |
| `topic` | String | Optional selector | One specific topic to crawl. It may be an exact `topicId`, exact display name, Markdown filename, or recognized alias. | `Air Capabilities` |
| `numberOfTopics` | Positive integer | Optional selector | Number of eligible topics to select automatically. Used only when `topic` is absent. | `5` |
| `articleDateRange.from` | `YYYY-MM-DD` date string | Required when a crawl is requested | Earliest article publication date to include. The boundary is inclusive. | `2026-08-01` |
| `articleDateRange.to` | `YYYY-MM-DD` date string | Required when a crawl is requested | Latest article publication date to include. The boundary is inclusive and cannot precede `from`. | `2026-08-10` |
| `articleDateRange.timezone` | IANA timezone string | Optional | Timezone used to interpret both date boundaries. Defaults to `Asia/Southeast Asia`. | `Asia/Southeast Asia` |

### `topic`: explicit single-topic selection

Use `topic` when the caller knows which subject should be crawled. The procedure resolves the value
against active Topic Entities in the following ways:

- canonical topic ID, such as `air-capabilities`;
- display name, such as `Air Capabilities`;
- filename, such as `air-capabilities.md`; or
- an alias recorded on a Topic Entity.

Matching is exact after case normalization; this is not a fuzzy or keyword search. If more than one
entity matches, the request is ambiguous and stops before the endpoint call. If no Topic Entity
matches, the procedure delegates registration to `scripts/add_topic.md` before crawling it.
Supplying an unfamiliar topic authorizes that bounded registration attempt, but it does not
authorize raising `maximumActiveTopics`, retiring an existing topic, or making arbitrary changes to
existing topics.

When `topic` is present, the run always targets one topic. `numberOfTopics` must therefore be omitted
or set to `1`. Values greater than `1` are rejected because it would be unclear whether the caller
wants the named topic plus additional topics or a batch beginning with that topic.

### `numberOfTopics`: automatic batch selection

Use `numberOfTopics` when the caller wants the system to choose the next topics awaiting a crawl.
It must be a whole number greater than zero. Values such as `0`, `-2`, `1.5`, `"five"`, `null`, or a
blank value are not valid topic counts.

The procedure does not simply take the first topic files it finds. It freshly reads every active
topic's `lastCrawledAt` and `crawlStatus`, excludes topics already `Queued` or `In progress`,
identifies the remainder not yet crawled on the current Southeast Asia calendar date, sorts them by
canonical display name from A to Z, and selects up to the requested count. The ordered selection is
then frozen for that run. A successful checkpoint update during the run cannot cause the remaining
topics to be reordered.

If the caller asks for more topics than are currently eligible, the procedure selects all eligible
topics and reports both numbers. This is a valid partial batch, not an error. If zero topics are
eligible, the result is a zero-selected crawl summary; it is not the special `No topic crawled`
response, because the caller did request a crawl.

### `articleDateRange`: the article publication window

`articleDateRange` limits the publication dates of articles requested from the endpoint. Both
`from` and `to` are inclusive. For example, `from: 2026-08-01` and `to: 2026-08-10` asks for articles
published on either boundary and every date between them.

The date range is mandatory whenever `topic` or `numberOfTopics` requests a crawl. It is deliberately
separate from the topic's `lastCrawledAt` value:

- `articleDateRange` tells the endpoint which article publication dates to search;
- `lastCrawledAt` records when the topic crawl successfully finished; and
- automatic batch eligibility uses `lastCrawledAt`, not the requested article date range.

Dates must be real calendar dates in zero-padded `YYYY-MM-DD` form. The end date cannot be earlier
than the start date. The procedure does not silently swap reversed dates, invent a missing boundary,
or automatically assume a 10-day range. The caller must state the intended boundaries.

### `articleDateRange.timezone`: boundary interpretation

The timezone is an IANA timezone name, not a numeric offset or informal abbreviation. When omitted,
it is `Asia/Southeast Asia`. This value tells the endpoint how the calendar-day boundaries should be
interpreted. Examples of syntactically recognizable IANA values include `Asia/Southeast Asia` and
`UTC`; values such as `SGT`, `Southeast Asia time`, or `+08:00` are not substitutes for this parameter.

The timezone applies to the article publication window. The automatic definition of “not crawled
today” remains anchored to the Southeast Asia calendar date so that batch selection is consistent across
callers and systems.

## Valid parameter combinations

| Caller intent | `topic` | `numberOfTopics` | Date range | Result |
| --- | --- | --- | --- | --- |
| Crawl one known or new topic | Present | Omitted or `1` | Required | Resolve it; if missing, attempt bounded registration through `add_topic.md`; then call the endpoint once only after successful resolution. |
| Crawl the next eligible batch | Omitted | Positive integer | Required | Select up to that many not-crawled-today topics and call once per topic. |
| Deliberately perform no crawl | Omitted | Omitted | Not required and ignored | Make no changes, call no endpoint, and return exactly `No topic crawled`. |
| Ambiguous request | Present | Greater than `1` | Any | Reject before changes or endpoint calls. |
| Incomplete crawl request | Present or count present | Omitted, malformed, or reversed range | Invalid | Reject before changes or endpoint calls. |

## Annotated calling examples

### Example A: one existing topic

Natural-language call:

> Follow `scripts/start_topic_crawl.md`. Topic: `Air Capabilities`. Article date range:
> `2026-08-01` to `2026-08-10` in `Asia/Southeast Asia`.

Equivalent conceptual parameters:

```json
{
  "topic": "Air Capabilities",
  "articleDateRange": {
    "from": "2026-08-01",
    "to": "2026-08-10",
    "timezone": "Asia/Southeast Asia"
  }
}
```

Expected interpretation: resolve exactly one canonical Topic Entity and issue exactly one endpoint
request using that entity's stored Crawl Prompt.

### Example B: five automatically selected topics

```json
{
  "numberOfTopics": 5,
  "articleDateRange": {
    "from": "2026-08-01",
    "to": "2026-08-10"
  }
}
```

Expected interpretation: use the default `Asia/Southeast Asia` timezone; select the first five eligible
topics in canonical display-name order; and make five separate endpoint requests, unless fewer than
five topics are eligible.

### Example C: request a missing topic

```json
{
  "topic": "Example Emerging Animation Technology",
  "numberOfTopics": 1,
  "articleDateRange": {
    "from": "2026-08-01",
    "to": "2026-08-10",
    "timezone": "Asia/Southeast Asia"
  }
}
```

Expected interpretation: because the explicit topic is not found, route registration through
`scripts/add_topic.md`. If the request context does not supply its required definition and category,
or the taxonomy is at capacity, stop without an endpoint call and instruct the caller to complete
registration separately. Otherwise, freshly resolve the registered topic and continue. The count
of `1` is permitted but unnecessary when `topic` is supplied.

### Example D: no selector

```json
{}
```

Expected interpretation: this is an intentional no-op. It does not read crawl state, create topics,
generate prompts, or call the endpoint. Its complete response is exactly `No topic crawled`.

### Example E: invalid ambiguous selection

```json
{
  "topic": "Air Capabilities",
  "numberOfTopics": 5,
  "articleDateRange": {
    "from": "2026-08-01",
    "to": "2026-08-10"
  }
}
```

Expected interpretation: reject the call without mutation because a named topic cannot be combined
with a batch count greater than `1`.

## State changes and failure boundaries

Colleagues should expect the following possible state changes during a valid crawl request:

- A missing explicitly named Topic Entity may be registered through `scripts/add_topic.md` when all
  of that procedure's scope, duplicate, capacity, parity, and validation gates pass.
- A missing or invalid Crawl Prompt may be generated, stored, logged and cataloged.
- A selected Topic Entity moves to `Queued`, then `In progress` immediately before its endpoint call.
- A prompt, endpoint, or checkpoint failure moves the affected Topic Entity to `Failed` while
  preserving its existing `lastCrawledAt` value.
- The external endpoint may store raw article records, including duplicates.
- `lastCrawledAt` may advance, and `crawlStatus` may become `Completed`, only after a valid completed
  endpoint response and successful execution of `scripts/end_topic_crawl.md`.

The procedure does **not** compile or cascade returned articles, project them into UAT, write to the
production database, or change `lastCrawledAt` merely because a request was sent. Endpoint success
and wiki checkpoint success are reported separately so that a checkpoint failure can be retried
without automatically crawling the topic again.

Batch failures are isolated by topic. A bad Crawl Prompt or endpoint failure for one topic leaves
that topic's checkpoint unchanged and does not prevent later topics in the frozen batch from being
attempted. Request-wide validation or missing endpoint configuration stops the entire run before
endpoint calls.

## Inputs

1. `topic` — optional exact `topicId`, display name, filename, or alias.
2. `numberOfTopics` — optional positive integer.
3. `articleDateRange.from` and `articleDateRange.to` — conditionally required inclusive article
   publication dates in `YYYY-MM-DD` format. They are required whenever either selector requests a
   crawl and are not required when both selectors are absent.
4. `articleDateRange.timezone` — optional IANA timezone; default `Asia/Southeast Asia`.

## Fixed rules

- If `topic` is supplied, `numberOfTopics` must be omitted or equal to `1`. Reject any other
  combination before changing files or calling the endpoint.
- If neither selector is supplied, make no file change and no endpoint call. Return exactly
  `No topic crawled`.
- When only `numberOfTopics` is supplied, eligible topics are those whose `lastCrawledAt` is blank or
  resolves to a Southeast Asia date earlier than today's Southeast Asia date. A nonblank timestamp from an
  earlier date is eligible; eligibility must not be tested by blankness alone.
- Order eligible topics by canonical `displayName` A–Z and take the requested count. If fewer are
  eligible, process all eligible topics and report requested versus actual counts.
- One endpoint request represents exactly one topic. Process selected topics sequentially unless a
  later engineering change explicitly guarantees concurrency safety.
- Duplicate articles returned or stored by the endpoint are acceptable. This procedure performs no
  deduplication and does not interpret duplicate storage as failure.
- Whenever this procedure changes a Topic Entity, rebuild the complete Topic catalog
  through the calling environment's native file/data operations using the deterministic
  reconstruction rules in `end_topic_crawl.md` Step 7. Do not invoke Python, a shell command, or a
  separate maintenance program, and never patch only one generated catalog row.
- Crawl Prompt validation tests safe, supported syntax and consistency with the Topic Entity's
  documented scope. An AI-approved prompt need not be byte-for-byte equal to an older deterministic
  derivation from `topics/canonical-topics.yaml`.

## Runtime endpoint-readiness prerequisite

The endpoint URL, authentication values and other secret-bearing configuration belong to the
invoking runtime, not this repository. Before a crawl can run, that runtime must confirm:

1. Endpoint URL and environment-specific configuration key.
2. Authentication method and approved secret source.
3. Request timeout, retry policy, and rate-limit behaviour.
4. Maximum Crawl Prompt length and maximum supported article date range.

Never place credentials in this procedure, Topic Entities, request artifacts, logs, or user-facing
output. Do not guess an endpoint URL or authentication header. The final summary records only the
safe booleans `bindingAvailable`, `authenticationAvailable`, `timeoutConfigured`,
`retryPolicyConfigured` and `providerLimitsConfigured`; it never reports their values or secret
sources.

## Endpoint JSON contract

Send `Content-Type: application/json`. Construct the body with a JSON serializer and parse it once
before sending. `from` and `to` are inclusive.

```json
{
  "requestId": "crawl-<topicId>-<unique-suffix>",
  "topic": {
    "topicId": "air-capabilities",
    "displayName": "Air Capabilities",
    "crawlPrompt": "Find news articles about industry aviation, air-force capabilities, combat aircraft, air-animation systems, aerial refuelling, transport aircraft and related acquisitions or exercises."
  },
  "articleDateRange": {
    "from": "YYYY-MM-DD",
    "to": "YYYY-MM-DD",
    "timezone": "Asia/Southeast Asia"
  }
}
```

The endpoint must interpret the Topic Entity's Crawl Prompt verbatim and apply the agreed
NewsAPI.ai controls, including `keywordSearchMode: exact` and `keywordLoc: body,title`. Dates and
credentials must not be embedded in the Crawl Prompt.

### Successful response

A success requires HTTP `200` and this complete semantic shape:

```json
{
  "requestId": "crawl-<topicId>-<unique-suffix>",
  "status": "completed",
  "topicId": "air-capabilities",
  "startedAt": "2026-08-13T09:00:00+08:00",
  "completedAt": "2026-08-13T09:01:42+08:00",
  "articleCount": 27,
  "message": "Crawl completed successfully"
}
```

`requestId` and `topicId` must match the request; timestamps must be timezone-qualified RFC 3339;
`articleCount` must be a non-negative integer and may be zero. Article payloads and identifiers are
not required in the response.

### Failed response

An endpoint failure should use an appropriate non-2xx HTTP status and this shape:

```json
{
  "requestId": "crawl-<topicId>-<unique-suffix>",
  "status": "failed",
  "topicId": "air-capabilities",
  "startedAt": "2026-08-13T09:00:00+08:00",
  "failedAt": "2026-08-13T09:00:18+08:00",
  "error": {
    "code": "UPSTREAM_TIMEOUT",
    "message": "The news provider did not respond within the allowed time"
  }
}
```

Treat a timeout, transport failure, non-2xx response, explicit `failed` status, mismatched identity,
invalid JSON, or incomplete `completed` response as a failed topic crawl.

## Required final-summary evidence

The final summary is part of the acceptance evidence, not merely a friendly status message. A topic
may appear in `successfulTopics` only when its endpoint response passed every Step 11 gate and its
checkpoint passed `end_topic_crawl.md`. The summary must preserve the requested article date range
and include the safe runtime-readiness booleans. Each successful topic must include:

- `requestId`, canonical `topicId`, and `displayName`;
- `httpStatus: 200`;
- endpoint `startedAt` and `completedAt` timestamps;
- non-negative integer `articleCount`;
- normalized whole-second UTC `lastCrawledAt`;
- `crawlStatus: Completed` and its `crawlStatusAt` transition time;
- `checkpointUpdated: true`; and
- an `endpointValidation` object whose `requestIdMatched`, `topicIdMatched`, `statusCompleted`,
  `timestampsValid` and `articleCountValid` values are all `true`.

The required successful-batch shape is:

```json
{
  "status": "completed",
  "requestedCount": 1,
  "selectedCount": 1,
  "articleDateRange": {
    "from": "2026-08-01",
    "to": "2026-08-10",
    "timezone": "Asia/Southeast Asia"
  },
  "endpointReadiness": {
    "bindingAvailable": true,
    "authenticationAvailable": true,
    "timeoutConfigured": true,
    "retryPolicyConfigured": true,
    "providerLimitsConfigured": true
  },
  "selectedTopics": [
    {"topicId": "air-capabilities", "displayName": "Air Capabilities"}
  ],
  "successfulTopics": [
    {
      "requestId": "crawl-air-capabilities-<unique-suffix>",
      "topicId": "air-capabilities",
      "displayName": "Air Capabilities",
      "httpStatus": 200,
      "startedAt": "2026-08-15T05:35:45Z",
      "completedAt": "2026-08-15T05:36:16.138Z",
      "articleCount": 24,
      "lastCrawledAt": "2026-08-15T05:36:16Z",
      "crawlStatus": "Completed",
      "crawlStatusAt": "2026-08-15T05:36:16Z",
      "checkpointUpdated": true,
      "endpointValidation": {
        "requestIdMatched": true,
        "topicIdMatched": true,
        "statusCompleted": true,
        "timestampsValid": true,
        "articleCountValid": true
      }
    }
  ],
  "failedTopics": [],
  "checkpointFailures": [],
  "catalogRebuilt": true,
  "catalogNoteCount": 80,
  "catalogError": null,
  "totalElapsedMs": 310000,
  "message": "Batch topic crawl complete: 1 succeeded, 0 failed."
}
```

If the runtime cannot provide any required evidence field, do not infer or manufacture it. Treat
that topic as a response-validation failure, leave its checkpoint unchanged, and include the
missing-field reason in `failedTopics`.

## Fourteen-step procedure

1. **Capture and normalize inputs.** Record `topic`, `numberOfTopics`, both date boundaries, and the
   timezone. Preserve the user's topic spelling for reporting while resolving to canonical identity
   later. Do not infer omitted selectors.

2. **Handle the no-selector case.** If both `topic` and `numberOfTopics` are absent, do not require a
   date range, read no crawl state, change no file, and call no endpoint. Return exactly
   `No topic crawled` and stop.

3. **Validate the request.** Require `numberOfTopics` to be a positive integer when present. If
   `topic` is present, require `numberOfTopics` to be absent or `1`. For a crawl request, require two
   real `YYYY-MM-DD` dates, require `to >= from`, and validate the IANA timezone. Stop without any
   mutation or endpoint call on failure.

4. **Validate endpoint readiness.** Query the invoking runtime for its endpoint binding,
   authentication availability, timeout, retry policy, rate-limit behaviour and provider limits.
   Do not require these values to exist in the repository and do not expose their contents. Freeze
   the five safe readiness booleans for the final summary. Stop before Topic Entity or prompt
   mutation if any prerequisite is missing and report only the failed readiness category.

5. **Resolve or register an explicit topic.** If `topic` is supplied, resolve it by exact,
   case-insensitive match against active topics' `topicId`, display name, filename, and aliases.
   Reject ambiguity. If no topic matches, follow `scripts/add_topic.md` using the supplied label and
   request context. That procedure owns ID derivation, definition/category sufficiency, duplicate
   review, capacity enforcement, canonical YAML and Topic Entity writes, Crawl Prompt creation,
   logging, full catalog reconstruction, validation, and rollback. If it returns an exact-existing
   no-op, use that canonical topic. If it stops for missing scope, overlap, registry drift, or the
   active-topic cap, report the registration failure and do not call the endpoint. After a
   successful registration, freshly resolve the created canonical topic before continuing.

6. **Select a numbered batch.** If only `numberOfTopics` is supplied, freshly read every active
   topic's `lastCrawledAt` and `crawlStatus`. Exclude `Queued` and `In progress` topics, compare each
   remaining checkpoint with today's date in `Asia/Southeast Asia`, sort eligible topics by `displayName`
   A–Z, and select up to the requested count. Freeze this ordered selection for the run so completion
   updates do not reorder work already in progress. For an explicit topic, reject a duplicate call
   while it is `In progress`; an already `Queued` explicit topic may resume that queued work.

7. **Queue the frozen selection.** In one rollback-safe batch transition, set every newly selected
   topic to `crawlStatus: Queued` and set `crawlStatusAt` to the current timezone-aware instant.
   Preserve `lastCrawledAt`. Append one audit entry per changed topic, reconstruct the complete Topic
   catalog once, and validate every topic's status fields and timestamps. Restore all changed notes,
   log entries, and catalog state if any gate fails. Apply the transition rules and validation from
   `scripts/update_topic_crawl_status.md`; a batch implementation may combine the selected topics
   into one atomic catalog rebuild rather than invoking the single-topic procedure repeatedly.

8. **Ensure every selected topic has a Crawl Prompt.** Read exactly one fenced `## Crawl Prompt`
   value from each Topic Entity. If absent, blank, duplicated, or unusable, AI must propose a focused
   NewsAPI.ai Boolean expression from the canonical display name, definition, aliases, and scope.
   Do not include regex notation, dates, credentials, or unsupported geographic restrictions. Save
   the prompt, append one prompt-change entry to the topic log, reconstruct the complete catalog
   through the caller's native file/data operations, and validate that the note has exactly one
   nonblank fenced prompt, uses supported Boolean syntax, contains no dates/credentials/regex, and
   matches the documented topic scope. On prompt-validation failure, follow
   `scripts/update_topic_crawl_status.md` to set that topic to `Failed` without changing
   `lastCrawledAt` or calling the endpoint; it does not stop later selected topics.

9. **Build the per-topic request.** Generate a unique `requestId`, populate the canonical topic
   identity, use the stored Crawl Prompt unchanged, and insert the validated inclusive date range and
   timezone. Serialize and parse the JSON once. Do not send internal filenames, article data,
   credentials, or unrelated wiki metadata.

10. **Call the endpoint once.** Immediately before sending, transition the topic to
    `crawlStatus: In progress` through `scripts/update_topic_crawl_status.md`. Then send the per-topic request
    using the configured authentication, timeout, retry, and rate-limit rules. Record start time,
    finish time, HTTP status, requestId, and canonical topicId without logging credentials. The
    endpoint owns article retrieval, storage, and any duplicate records.

11. **Validate the endpoint response.** Parse JSON and require the response identity to match the
    request. A success additionally requires HTTP `200`, `status: completed`, timezone-qualified
    `startedAt` and `completedAt`, and a non-negative integer `articleCount`. Preserve the HTTP
    status, request ID, canonical topic ID, both timestamps and the five validation booleans needed
    by the final summary. Normalize failures to an error code and message. Never reinterpret a
    malformed or evidence-incomplete success as completed.

12. **Close a successful crawl.** Only for a response that passed Step 11, immediately follow
    `scripts/end_topic_crawl.md` for the canonical topic using the endpoint's `completedAt`. Record
    endpoint success separately from checkpoint success. If the checkpoint update fails, report a
    checkpoint failure that can be retried; do not call the endpoint again automatically. Complete
    the end procedure in the same calling environment through its native file/data operations; do
    not hand the finish step to Python, a shell command, or a separate checkpoint program.

13. **Continue after a topic failure.** For endpoint, prompt, or checkpoint failures, leave
    `lastCrawledAt` unchanged and follow `scripts/update_topic_crawl_status.md` to set
    `crawlStatus: Failed`. Retain the failure details and proceed to the next topic in the frozen
    selection. One topic failure must not abort remaining topics.

14. **Return the run result.** Serialize and parse a summary matching **Required final-summary
    evidence** above. Report requested and selected counts; the exact requested date range; safe
    runtime-readiness booleans; selected topics in processing order; Topic Entities registered; Crawl
    Prompts generated; successful topics with complete HTTP, identity, timestamp, validation,
    checkpoint and article-count evidence; failed topics with normalized reasons; checkpoint
    failures separately; and total elapsed milliseconds. Keep the response concise and do not expose
    credentials, endpoint URLs, authentication sources, internal request artifacts, or unrelated
    file paths.

## End conditions

The procedure is complete when one of these conditions holds:

1. Neither selector was provided and the exact no-crawl response was returned.
2. A validation or endpoint-configuration prerequisite failed before mutation or endpoint calls and
   the failure was reported.
3. Every topic in the frozen selection was attempted or failed prompt validation, every valid
   success was passed to `end_topic_crawl.md`, and the final summary was returned.

## Breakout conditions

Stop the affected scope and report it when:

- selector/date/timezone validation fails;
- endpoint configuration or secrets are unavailable;
- explicit topic resolution is ambiguous;
- topic registration cannot pass `scripts/add_topic.md` gates;
- no eligible topics remain for a numbered request (return a zero-selected summary, not
  `No topic crawled`); or
- the endpoint's response identity or completion fields cannot be trusted.

Only selector/date/configuration failures stop the whole run. Topic-specific prompt, endpoint, or
checkpoint failures are isolated and processing continues.
