#!/usr/bin/env python3
"""Mechanically patch an existing entity's ## Coverage list and count field.

This is the "bookkeeping" half of Step 3 (existing-entity branch) of
entity_cascade_procedure.md — it does NOT decide which entities are new,
does NOT write ## Summary prose, and does NOT touch ## Related Entities.
Those are judgment calls and stay manual. This script only does the
mechanical, error-prone part: incrementing mentionCount/articleCount by
exactly the number of genuinely-new (entity, article) pairs, and appending
the matching `- [[article|label]]` lines — the exact bookkeeping that
caused the Ng Eng Hen overcount and the articleCount double-counting bug
earlier this session.

Input: a JSON file, a list of update objects:
  {"domain": "organisations", "id": "studio-sector",
   "article": "1004217-head-of-civil-service-leo-yip-to-retire-permanent",
   "label": "Head of Civil Service Leo Yip to retire, Chan Heng Kee succeeds",
   "alias": "Studio Sector"}                      <- optional, outlet/country/place only

Multiple entries for the same (domain, id) are grouped automatically, so you
can list every (entity, article) pair from a whole batch in one file and
run this once.

Idempotent: if an article is already linked in an entity's Coverage block,
that pair is silently skipped (not re-added, not re-counted) — safe to
re-run after a partial/interrupted batch.

`outlet` is special-cased per the fix-articlecount-double-counting decision:
articleCount is NEVER incremented (it's a pre-computed corpus-wide total),
only Coverage lines and a missing `aliases` field are added.

Usage:
  python3 patch_coverage.py updates.json           # apply
  python3 patch_coverage.py updates.json --dry-run  # preview only
  python3 patch_coverage.py updates.json --no-run-log # suppress receipt
"""
import sys
import os
import re
import json
from collections import defaultdict
from contextlib import nullcontext
from run_logger import RunLogger

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTITIES_DIR = os.path.join(ROOT, "entities")

# Which frontmatter field this domain's mention count lives in.
# None means "never increment" (outlet: articleCount is a pre-computed
# corpus-wide total, per fix-articlecount-double-counting.md).
COUNT_FIELD = {
    "organisations": "mentionCount",
    "people": "mentionCount",
    "country": "mentionCount",
    "place": "mentionCount",
    "topic": "articleCount",
    "tag": "articleCount",
    "outlet": None,
}


def load_updates(path):
    """Load update rows and group them by target entity.

    Grouping keeps count updates correct when multiple new articles mention the
    same entity in one cascade batch.
    """
    with open(path, encoding="utf-8") as f:
        updates = json.load(f)
    grouped = defaultdict(list)
    for u in updates:
        domain = u["domain"]
        if domain not in COUNT_FIELD:
            raise ValueError(f"Unknown domain {domain!r} in update {u!r}")
        grouped[(domain, u["id"])].append(u)
    return grouped


def find_entity_file(domain, entity_id):
    """Return the canonical note path for an existing entity, if present."""
    path = os.path.join(ENTITIES_DIR, domain, f"{entity_id}.md")
    return path if os.path.exists(path) else None


def find_coverage_block(text):
    """Return (start_idx, end_idx, block_text) spanning the '## Coverage'
    section (header line through the line before the next '## ' header,
    or EOF). Returns None if no ## Coverage header exists."""
    m = re.search(r"^## Coverage\s*$", text, re.MULTILINE)
    if not m:
        return None
    start = m.end()
    next_header = re.search(r"^## ", text[start:], re.MULTILINE)
    end = start + next_header.start() if next_header else len(text)
    return start, end, text[start:end]


def apply_update(domain, entity_id, updates, dry_run):
    """Apply all new Coverage lines for one entity.

    Existing article links are detected inside the current Coverage block and
    skipped before counts are incremented, which makes reruns idempotent after
    an interrupted cascade.
    """
    path = find_entity_file(domain, entity_id)
    if path is None:
        return {"domain": domain, "id": entity_id, "error": "entity file not found — create it manually first"}

    with open(path, encoding="utf-8") as f:
        text = f.read()

    if not text.startswith("---"):
        return {"domain": domain, "id": entity_id, "error": "no frontmatter block found"}
    fm_end = text.find("\n---", 3)
    if fm_end == -1:
        return {"domain": domain, "id": entity_id, "error": "malformed frontmatter block"}
    fm_end += len("\n---")
    frontmatter = text[:fm_end]
    rest = text[fm_end:]

    cov = find_coverage_block(text)
    existing_block = cov[2] if cov else ""

    new_lines = []
    skipped = []
    for u in updates:
        article = u["article"]
        if f"[[{article}" in existing_block or f"[[{article}" in "\n".join(new_lines):
            skipped.append(article)
            continue
        label = u.get("label")
        line = f"- [[{article}|{label}]]" if label else f"- [[{article}]]"
        new_lines.append(line)

    field = COUNT_FIELD[domain]
    old_count = None
    new_count = None
    if field:
        fm_match = re.search(rf"^{field}: (\d+)\s*$", frontmatter, re.MULTILINE)
        if not fm_match:
            return {"domain": domain, "id": entity_id, "error": f"no {field} field found in frontmatter"}
        old_count = int(fm_match.group(1))
        new_count = old_count + len(new_lines)
        frontmatter = frontmatter[:fm_match.start()] + f"{field}: {new_count}" + frontmatter[fm_match.end():]

    # Optional alias backfill (outlet notes, mainly) — only if missing entirely.
    alias_added = None
    alias_candidates = [u.get("alias") for u in updates if u.get("alias")]
    if alias_candidates and "\naliases:" not in frontmatter and not frontmatter.startswith("aliases:"):
        alias = alias_candidates[0]
        frontmatter = frontmatter.rstrip("\n").removesuffix("---")
        frontmatter = frontmatter.rstrip() + f"\naliases: [{alias}]\n---"
        alias_added = alias

    if not new_lines and not alias_added:
        return {
            "domain": domain, "id": entity_id, "old_count": old_count, "new_count": old_count,
            "added": [], "skipped": skipped, "alias_added": None, "noop": True,
        }

    if cov and new_lines:
        start, end, _ = cov
        # rest is text[fm_end:]; recompute offsets relative to rest since frontmatter may have changed length
        rel_start = start - fm_end
        rel_end = end - fm_end
        block_lines = rest[rel_start:rel_end].split("\n")
        # Insert right after the last existing "- [[...]]" entry, not at the
        # very end of the block — the block also captures the blank
        # separator line(s) before the next "## " header, and appending
        # past those would split the list with a gap and swallow the
        # separator before the next header.
        last_entry = -1
        for i, ln in enumerate(block_lines):
            if ln.strip().startswith("- "):
                last_entry = i
        insert_at = last_entry + 1  # -1 -> 0 when the block has no entries yet
        block_lines = block_lines[:insert_at] + new_lines + block_lines[insert_at:]
        rest = rest[:rel_start] + "\n".join(block_lines) + rest[rel_end:]
    elif not cov and new_lines:
        # No ## Coverage section at all yet — append one at end of file.
        rest = rest.rstrip("\n") + "\n\n## Coverage\n" + "\n".join(new_lines) + "\n"

    new_text = frontmatter + rest

    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)

    return {
        "domain": domain, "id": entity_id, "old_count": old_count, "new_count": new_count,
        "added": [u["article"] for u in updates if u["article"] not in skipped],
        "skipped": skipped, "alias_added": alias_added, "noop": False,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 patch_coverage.py updates.json [--dry-run] [--no-run-log]", file=sys.stderr)
        sys.exit(1)
    updates_path = sys.argv[1]
    dry_run = "--dry-run" in sys.argv[2:]
    no_run_log = "--no-run-log" in sys.argv[2:]

    grouped = load_updates(updates_path)
    update_rows = sum(len(v) for v in grouped.values())
    article_count = len({u["article"] for updates in grouped.values() for u in updates})
    run = None
    if not no_run_log:
        run = RunLogger(
            "cascade",
            metadata={
                "script": "patch_coverage.py",
                "dryRun": dry_run,
                "updatesPath": os.path.relpath(os.path.abspath(updates_path), ROOT),
            },
        )

    results = []
    with (run.stage("patch_coverage", article_count=article_count, file_count=len(grouped)) if run else nullcontext()):
        for (domain, entity_id), updates in grouped.items():
            results.append(apply_update(domain, entity_id, updates, dry_run))

    errors = [r for r in results if "error" in r]
    changed = [r for r in results if "error" not in r and not r["noop"]]
    noops = [r for r in results if "error" not in r and r["noop"]]

    label = "DRY RUN — no files written" if dry_run else "APPLIED"
    print(f"=== {label} ===")
    for r in changed:
        count_str = f"{r['old_count']} → {r['new_count']}" if r["old_count"] is not None else "(not tracked)"
        print(f"[{r['domain']}/{r['id']}] count {count_str}, +{len(r['added'])} Coverage line(s)"
              + (f", alias added: {r['alias_added']}" if r["alias_added"] else "")
              + (f", skipped (already present): {r['skipped']}" if r["skipped"] else ""))
    for r in noops:
        print(f"[{r['domain']}/{r['id']}] no-op — all {len(r['skipped'])} article(s) already in Coverage")
    if errors:
        print("\n=== ERRORS ===")
        for r in errors:
            print(f"[{r['domain']}/{r['id']}] {r['error']}")

    print(f"\n{len(changed)} entities updated, {len(noops)} no-ops, {len(errors)} errors "
          f"({len(grouped)} entities total from {update_rows} update rows).")

    if run:
        added_pairs = sum(len(r["added"]) for r in changed)
        skipped_pairs = sum(len(r["skipped"]) for r in changed + noops)
        run.status = "failed" if errors else "ok"
        run.set_article_metrics(
            inputCount=article_count,
            processedCount=article_count,
            updatedCount=added_pairs,
            skippedCount=skipped_pairs,
            failedCount=len(errors),
        )
        run.set_file_metrics(
            scannedCount=len(grouped),
            changedCount=len(changed),
            failedCount=len(errors),
        )
        run.add_output("updateRows", update_rows)
        run.add_output("entitiesChanged", len(changed))
        run.add_output("entitiesNoop", len(noops))
        run.add_output("coveragePairsAdded", added_pairs)
        run.add_output("coveragePairsSkipped", skipped_pairs)
        for error in errors:
            run.add_error(error["error"], domain=error["domain"], entityId=error["id"])
        if errors:
            run.status = "failed"
        run.finish()

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
