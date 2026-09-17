---
type: procedure
name: add-topic
status: active
last_updated: 2026-08-19
---

# Add Topic Procedure

Use this procedure to register one new canonical monitoring topic in both sources that define the
active taxonomy:

1. `topics/canonical-topics.yaml` — the machine-readable assignment and crawl registry; and
2. `entities/topic/<topicId>.md` — the Markdown source-of-truth Topic Entity.

Registration does not crawl the topic, alter article assignments, cascade coverage, prepare a UAT
delta, or write to any database. To crawl the topic after registration, separately follow
`scripts/topic_crawl_plan.md` with an article date range.

> **USER OR CALLER INSTRUCTIONS — use this form:**
>
> “Follow `scripts/add_topic.md`. Display name: `<name>`. Definition: `<scope and exclusion
> boundary>`. Category: `<approved category>`. Aliases: `<optional list>`. Keywords: `<optional
> canonical assignment seeds>`. Crawl Prompt: `<optional NewsAPI.ai Boolean expression>`.”

## Inputs

1. `displayName` — required nonblank canonical label.
2. `definition` — required concise statement of what the topic covers and, where ambiguity is
   plausible, what it excludes. It must be grounded in the caller's request or saved vault evidence.
3. `category` — required exact category already used by the active canonical registry.
4. `topicId` — optional proposed lowercase kebab-case stable ID. When omitted, derive it
   deterministically from `displayName`.
5. `aliases` — optional exact synonyms that should resolve to this topic.
6. `keywords` — optional assignment seeds for `topics/canonical-topics.yaml`. When omitted, derive
   a minimal set only from the approved display name, aliases, and definition.
7. `crawlPrompt` — optional executable NewsAPI.ai Boolean expression. When omitted, propose one
   only from the approved display name, aliases, definition, and keywords.

Do not use a live web lookup or model background knowledge to broaden the supplied scope. If the
request does not provide enough information to state a stable classification boundary, stop before
mutation and request the missing definition or category.

## Fixed rules

- The active vocabulary remains bounded by `maximumActiveTopics` in
  `topics/canonical-topics.yaml`. Never increase that value as an incidental part of registration.
  If the active count already equals the maximum, stop without mutation. The caller must first
  approve and complete a topic merge/retirement, or authorize a separate governance decision that
  changes the cap.
- Prefer an existing topic or alias whenever its scope already covers the request. Do not create a
  near-duplicate merely to preserve different wording.
- `topicId` is immutable after registration, equals the filename stem, and uses lowercase ASCII
  kebab-case with no doubled slug.
- A new Topic Entity starts with `articleCount: 0`, `lastCrawledAt: null`,
  `crawlStatus: Not started`, and `crawlStatusAt: null`.
- Every active topic has exactly one nonblank `## Crawl Prompt` fenced `text` block. The expression
  may use the provider's supported Boolean/proximity syntax, but never regex notation, dates,
  credentials, endpoint values, or operational pagination/source controls.
- Every active topic has one `## Source Profile` section. Advisory sources may define targeted
  crawl lanes, but must never filter broad topic discovery.
- `Coverage` is empty at registration. Do not invent backlinks or recalculate article assignments.
- `entities/topic/catalog.md` is generated state. Reconstruct it in full; never patch a single row.
- Registration is local-only. It must not project to UAT or write to production.

## Procedure

1. **Read the governing records.** Read `README.md`, `entities/topic/index.md`,
   `topics/canonical-topics.yaml`, the active direct Topic notes, and the relevant accepted Topic
   decisions. Exclude `index.md`, `catalog.md`, `log.md`, `_template.md`, files beginning `log-`,
   and Dropbox conflicted-copy files from the active note set.

2. **Validate the request.** Require the inputs above, normalize surrounding whitespace, and reject
   blank list members. Require `category` to exactly match an existing registry category. Derive or
   validate `topicId`; require filename-safe lowercase kebab-case and reject reserved system names.

3. **Prove the two current registries agree.** Parse the canonical YAML and every active Topic
   note. Require unique, nonblank topic IDs; require every active filename to equal
   `<topicId>.md`; and require the Topic Entity ID set to equal the canonical YAML ID set. Stop on
   malformed YAML, duplicate IDs, missing counterparts, or extra counterparts. Report ignored
   conflicted-copy artifacts separately, and stop if the proposed identity collides with one. Do not
   repair pre-existing drift as part of this procedure.

4. **Resolve duplicates before minting an ID.** Match the proposed ID, display name, filename, and
   aliases case-insensitively against every active topic's ID, display name, filename, and aliases.
   Also compare the proposed definition, keywords, and classification boundary with existing
   topics. If there is one exact existing match, return that canonical topic as an idempotent no-op.
   If there is any ID/alias collision or plausible scope overlap, stop and report the candidate
   topics; require an explicit merge, alias, or distinct-boundary decision before registration.

5. **Enforce the bounded taxonomy.** Count the agreed active ID set and read
   `maximumActiveTopics`. Require `activeCount < maximumActiveTopics`. Never edit the maximum in
   this operation. Record the pre-add and proposed post-add counts for validation and reporting.

6. **Build the complete proposed record.** Produce one in-memory registration object containing
   `topicId`, `displayName`, `category`, unique aliases, unique keyword seeds, definition, and Crawl
   Prompt and Source Profile. Keep the definition concise. Keep keyword seeds narrow enough for canonical assignment;
   escaped regex-style seeds already supported by the assignment registry are permitted there, but
   regex is forbidden in the Crawl Prompt. Validate balanced quotes/parentheses and supported
   Boolean syntax in the prompt.

7. **Take a rollback snapshot.** Preserve the complete current contents of
   `topics/canonical-topics.yaml`, `entities/topic/log.md`, and `entities/topic/catalog.md` when it
   exists, plus the fact that the target Topic note does not exist. These snapshots are temporary
   recovery state, not an approval preview.

8. **Write both source records as one transaction.** Insert exactly one mapping in
   `topics/canonical-topics.yaml` without changing `schemaVersion`, `maximumActiveTopics`,
   `assignmentLimit`, or existing topic records. Create `entities/topic/<topicId>.md` from the Topic
   Template with the approved definition, one Crawl Prompt, a Source Profile, an empty Coverage
   section, and no invented Notes. Do not create either record unless both can be completed.

9. **Append one Topic audit entry.** Append exactly one line to `entities/topic/log.md` containing a
   timezone-aware timestamp, `[[<topicId>|<displayName>]]`, action `registered`, category, aliases,
   post-add active count, and `source: scripts/add_topic.md`. Never rewrite an earlier entry.

10. **Reconstruct the Topic catalog.** Enumerate the complete active direct-note set using the same
    exclusions as Step 1. Rebuild `entities/topic/catalog.md` in full using registry field order
    `topicId`, `displayName`, `category`, `aliases`, `articleCount`, `lastCrawledAt`, `crawlStatus`,
    `crawlStatusAt`, followed by `File`; sort by `displayName` case-insensitively; set both displayed
    counts from the records; and atomically replace the catalog.

11. **Validate before committing.** Re-read every touched file and require all of the following:

    - canonical YAML and Topic note frontmatter parse successfully;
    - the post-add canonical YAML and active-note ID sets are equal and contain the new ID once;
    - the post-add count equals `preAddCount + 1` and does not exceed `maximumActiveTopics`;
    - the target filename, `topicId`, display name, category, aliases, definition, keywords, and
      prompt exactly match the approved registration object;
    - the target note has the four required initial count/checkpoint/status values;
    - exactly one `## Definition`, `## Crawl Prompt`, `## Coverage`, and `## Notes` section exists;
    - the prompt is nonblank, supported, scope-consistent, and contains no dates or credentials;
    - the catalog count and IDs equal the active canonical note set and contain one matching target
      row;
    - exactly one matching audit entry was appended; and
    - no newly broken wikilink or malformed Markdown was introduced.

12. **Rollback on failure.** If any write or validation gate fails, restore every snapshotted file
    and remove only the newly created target note. Report the failed gate and do not claim that the
    topic was registered.

13. **Report completion.** Only after all gates pass, report the canonical ID, display name,
    category, aliases, active count versus maximum, Topic note path, registry path, and
    `crawlStatus: Not started`. State explicitly that no crawl, article reassignment, UAT
    projection, or production write occurred.

## Runtime requirement

This procedure is runtime-neutral. The caller must provide atomic replacement, complete catalog
reconstruction, validation, and snapshot restoration. It may use safe native file/data operations
or existing repository maintenance utilities, provided their behaviour satisfies every gate above.

## Boundaries

- Use `scripts/topic_list.md` to inspect the current roster without mutation.
- Use this procedure for registration only.
- Use `scripts/topic_crawl_plan.md` to crawl a registered topic.
- Use `scripts/update_topic_crawl_status.md` or `scripts/end_topic_crawl.md` for crawl state.
- Topic merge, retirement, article reassignment, cap changes, UAT projection, and production writes
  require their own authorized workflows.
