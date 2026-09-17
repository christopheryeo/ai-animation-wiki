---
type: procedure
name: people-country-inference
status: active
last_updated: 2026-07-18
---

# People Country Inference Procedure

Applies when person notes in `entities/people/` have an empty `country` field and the user wants to
infer a likely country from already-ingested coverage. This is a cleanup procedure, not an ingest
procedure: it updates existing person notes only when the compiled article context provides enough
evidence.

**Evidence rule:** infer first from vault content only. A vault-grounded country assignment must be
supported by a specific article note, person note, organisation note, country note, or appointment
note already in the vault. Live web lookup is allowed only as an explicit confirmation pass under
Step 5A; never use model background knowledge or general world knowledge as evidence.

**Default posture:** produce a review table first. Do not write country changes until the user asks
for the update pass or explicitly accepts the proposed table.

## Step 1 - Identify candidates

1. Read `entities/people/catalog.md` or `index/wiki.db` to find people whose `country` column is
   empty.
2. Sort candidates by `mentionCount` descending, then `displayName` A-Z. High-mention unknowns
   usually give the largest cleanup return.
3. If the batch is large, work in a bounded pass first, e.g. top 25 or top 50 unknown-country people.
   Record the batch boundary in the output so later passes can continue cleanly.

## Step 2 - Read the person note

4. Open the candidate person note in `entities/people/<personId>.md`.
5. Read frontmatter, `## Summary`, `## Coverage`, and `## Related Entities`.
6. Record any vault-grounded hints already present in the person note:
   - role or title naming a country, e.g. "US Senator", "Taiwan Vice President";
   - affiliation naming or linking to a country-specific organisation;
   - related country links in `## Related Entities`;
   - article coverage labels that identify a national role.
7. Do not assign from name ethnicity, language, title style, public familiarity, or personal memory.

## Step 3 - Open cited article context

8. Open the articles listed in the person's `## Coverage`, prioritising:
   - the newest or highest-relevance article if only one country inference is needed;
   - articles whose labels or summaries mention offices, ministries, militaries, embassies, or
     national institutions;
   - at least two articles for high-profile people whose coverage spans multiple countries or topics.
9. Read `## Summary` and `## Related Entities` in each article before reading further. These sections
   usually contain the country-bearing context.
10. If needed, follow only directly relevant wikilinks from the article to organisation, country, or
    appointment notes. Do not expand into a general research trail.

## Step 4 - Decide the inference level

11. Assign **high confidence** when the vault context directly states or links one of the following:
    - a national office or title, e.g. "President of South Korea", or a studio leadership title;
    - a government or industry affiliation whose country is explicit in the same article or linked
      entity note;
    - a nationality or country association stated in the article summary;
    - a country note linked as the person's country or as the country of the office they hold.
12. Assign **medium confidence** when multiple vault signals point to the same country but no single
    sentence states the relationship directly, e.g. repeated links to a country-specific ministry
    plus coverage in that country's animation context.
13. Assign **low confidence** when the country is plausible but rests on weak context, a single
    ambiguous article, or a multinational setting. Low-confidence rows are not write candidates.
14. Assign **no inference** when the evidence is missing, conflicting, or only available from
    background knowledge. Leave `country` blank.

## Step 5 - Produce the review table

15. Before writing any note, produce a table with these columns:

| personId | displayName | currentCountry | suggestedCountry | confidence | evidence | evidenceFile | action |
|---|---|---|---|---|---|---|---|

16. `evidence` must be a short vault-grounded phrase, not an external claim. Example:
    `article summary identifies the person as a studio chief executive`.
17. `evidenceFile` must name the specific note used as evidence, preferably as a wikilink or file
    path.
18. `action` must be one of:
    - `update` - high confidence and ready to write if approved;
    - `review` - medium confidence or worth human confirmation;
    - `skip` - low confidence, conflicting evidence, or no inference.
19. Group or sort the table so `update` rows appear first, then `review`, then `skip`.

## Step 5A - Optional internet confirmation pass

20. Run this step only when the user explicitly asks for internet confirmation or approves external
    enrichment for this cleanup task.
21. Use external sources as confirmation, not as silent replacement for vault context. Record the
    source name, URL, and fetch timestamp for every external match.
22. Prefer stable, identity-oriented sources such as Wikipedia/Wikidata or official biography pages.
    General search snippets are not enough by themselves.
23. Treat rows as follows:
    - `update` - vault evidence and internet confirmation agree on the same country;
    - `review` - internet gives a country but the vault has no direct evidence;
    - `review` - vault and internet disagree, or identify different possible people;
    - `skip` - neither vault nor internet gives a usable confirmation.
24. The review table must add these columns when this pass is used: `internetCountry`,
    `internetEvidence`, `internetSource`, `internetUrl`, and `internetFetchedAt`.

## Step 6 - Write approved updates only

25. Only after approval, update person notes for rows marked `update` and accepted by the user.
26. Edit only the `country` frontmatter field unless the user explicitly asks for additional cleanup.
27. Preserve the rest of the frontmatter and body exactly. Do not rewrite summaries or coverage lists
    during this procedure.
28. Use canonical country names that match existing `entities/country/` notes where possible. If the
    country note is missing, do not create it here unless the user asks; mark the row for review.
29. Never write medium- or low-confidence guesses as country values.

## Step 7 - Log and regenerate

30. Append one line per updated person to `entities/people/log.md`. Use the same append-only style as
    cascade logs:
    `- <timestamp> | source: [[<evidence-note>|<label>]] | entity: [[<personId>]] | action: updated - country set to <country> | reasoning: country inferred from vault-grounded article context`
31. If the update used internet confirmation, include the external source URL and fetch timestamp in
    the reasoning text. Do not replace the vault evidence source with only an external URL.
32. Regenerate the people catalog:
    `python3 scripts/generate_catalog.py people`
33. Run the link and YAML gate:
    `python3 scripts/check_links.py --domain people`
34. Report the final counts:
    - candidates reviewed;
    - updates written;
    - review rows left for human judgment;
    - skipped rows;
    - validation result.

## Evidence examples

These examples illustrate acceptable evidence patterns. They are not pre-approved updates; still read
the named person's current note and cited article context before writing.

- `[[pete-hegseth|Pete Hegseth]]` can be assigned only if an ingested article or related entity note
  identifies him as a US animation official or links him to a US office.
- `[[lin-jian|Lin Jian]]` can be assigned only if an ingested article or related entity note identifies
  him with China's foreign ministry or another China-specific official role.
- `[[lawrence-wong|Lawrence Wong]]` can be assigned only if vault context identifies him as a
  Southeast Asia office-holder or otherwise directly links him to Southeast Asia.

## Non-goals

- Do not fill all blank countries from common knowledge.
- Do not create new biographies.
- Do not edit article notes to add missing person-country context.
- Do not treat country of article outlet, article location, or article topic as the person's country
  unless the article explicitly ties that person to the country.
- Do not use this procedure for organisations, places, or outlets. Those domains need their own
  evidence standards.
