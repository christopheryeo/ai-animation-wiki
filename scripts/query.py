#!/usr/bin/env python3
"""Entry point for the entity Query procedure (`scripts/query_procedure.md`).

Two ways to run a query — no mock/offline stub:

  1. API mode (autonomous; uses the model via OPENAI_API_KEY). This is what an
     external UI + n8n calls. `query.py ask "..."` or `query.py serve`. The model
     reads `query_procedure.md` and drives the deterministic vault tools itself,
     returning a structured answer.

  2. Claude-assisted mode (no API key). The deterministic steps are exposed as
     individual commands — `resolve`, `read`, `search`, `list`, `submit` — so an
     agent (e.g. Claude in a Cowork session) can drive the procedure by hand:
     resolve the entity, read its note, reason, then `submit` the answer to the
     cache. Useful where there is no outbound API access. Not autonomous — it
     needs the agent in the loop, so it is not a path n8n can trigger.

Both modes operate on the *existing* vault (no separate test vault). Two
independent switches, precedence per-request arg > env var > default (on):
    cache_read   — Step 0: check the search cache, short-circuit on a fresh hit.
    cache_write  — Steps 6-7: file the Q&A into entities/search/ + regen catalog.

The API call matches the house style of `scripts/enrich_radar_inputs.py`:
OpenAI Responses API over stdlib urllib, OPENAI_API_KEY via load_local_env().

CLI:
    python3 query.py ask "your question" [--no-cache-read] [--no-cache-write] [--json]
    python3 query.py serve [--port 8080]          # HTTP endpoint for n8n
    python3 query.py resolve "CDF" [--domains appointments people]
    python3 query.py read appointments cdf
    python3 query.py search "what was said about X?"
    python3 query.py list appointments [--limit 400]
    python3 query.py submit --question "..." --answer "..." \
            [--entities appointments/cdf ...] [--sources 782384 ...] \
            [--animation-industry] [--time-sensitive] [--reuse <cached-queryId>]
    python3 query.py "your question"              # shorthand for `ask`
"""
from __future__ import annotations

import argparse
from collections import Counter
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from local_env import load_local_env  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - environment guard
    print("This script requires PyYAML. Install it with: pip3 install pyyaml", file=sys.stderr)
    sys.exit(2)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
ENTITIES = ROOT / "entities"
SEARCH_DIR = ENTITIES / "search"
PROCEDURE_PATH = SCRIPTS / "query_procedure.md"

API_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("QUERY_MODEL", "gpt-5.6")
MAX_TOOL_TURNS = 24
API_TIMEOUT = 120
NOTE_CHAR_CAP = 12000
ROSTER_ROW_CAP = 400
RESOLVE_MATCH_CAP = 25
FAST_CONTEXT_CHAR_CAP = 18000
FAST_EVIDENCE_CAP = 12
FAST_DOMAINS = (
    "appointments", "people", "organisations", "place",
    "country", "topic", "outlet", "issues",
)


class QueryError(RuntimeError):
    """Raised for unrecoverable problems while answering a query."""


# --------------------------------------------------------------------------- #
# Config / flags
# --------------------------------------------------------------------------- #
def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def resolve_flags(cache_read: bool | None, cache_write: bool | None) -> tuple[bool, bool]:
    """Per-request argument > env var > hard default (both on for production)."""
    read = cache_read if cache_read is not None else _env_flag("QUERY_CACHE_READ", True)
    write = cache_write if cache_write is not None else _env_flag("QUERY_CACHE_WRITE", True)
    return read, write


def fast_path_enabled() -> bool:
    """Return whether the reversible deterministic query fast path is enabled."""
    return _env_flag("QUERY_FAST_PATH", True)


# --------------------------------------------------------------------------- #
# Vault helpers (deterministic; no model involved)
# --------------------------------------------------------------------------- #
_CATALOG_CACHE: dict[str, tuple[list[str], list[dict[str, str]]]] = {}


def _domains_with_catalog() -> list[str]:
    out = []
    for path in sorted(ENTITIES.glob("*/catalog.md")):
        out.append(path.parent.name)
    return out


def _strip_file_link(cell: str) -> str:
    """`[chan-chun-sing.md](chan-chun-sing.md)` -> `chan-chun-sing.md`."""
    m = re.search(r"\(([^)]+)\)", cell)
    return (m.group(1) if m else cell).strip()


def parse_catalog(domain: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse entities/<domain>/catalog.md's markdown table into header + rows."""
    if domain in _CATALOG_CACHE:
        return _CATALOG_CACHE[domain]
    path = ENTITIES / domain / "catalog.md"
    columns: list[str] = []
    rows: list[dict[str, str]] = []
    if not path.exists():
        _CATALOG_CACHE[domain] = (columns, rows)
        return columns, rows
    table_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.lstrip().startswith("|")]
    if len(table_lines) >= 2:
        columns = [c.strip() for c in table_lines[0].strip().strip("|").split("|")]
        for ln in table_lines[2:]:  # skip header + separator
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if len(cells) != len(columns):
                continue
            rows.append(dict(zip(columns, cells)))
    _CATALOG_CACHE[domain] = (columns, rows)
    return columns, rows


def _row_id_and_label(domain: str, row: dict[str, str]) -> tuple[str, str, str]:
    """Return (id, label, file) for a catalog row, tolerant of per-domain columns."""
    file = _strip_file_link(row.get("File", "")) if "File" in row else ""
    ident = ""
    for key in ("appointmentId", "queryId", "id", "slug"):
        if row.get(key):
            ident = row[key]
            break
    if not ident and file:
        ident = file[:-3] if file.endswith(".md") else file
    if not ident:
        # fall back to first column value
        ident = next(iter(row.values()), "")
    label = row.get("displayName") or row.get("query") or ident
    return ident, label, file


def tool_resolve_entity(name: str, domains: list[str] | None = None) -> dict[str, Any]:
    """Deterministic candidate lookup: match `name` against catalog rows.

    Matches (case-insensitively) against a row's id / displayName / aliases /
    acronyms cells across every entity domain (or the subset given). Returns
    candidate records only — disambiguation is the caller's job (Step 1).
    """
    needle = name.strip().lower()
    search_domains = domains or _domains_with_catalog()
    matches: list[dict[str, str]] = []
    for domain in search_domains:
        _, rows = parse_catalog(domain)
        for row in rows:
            hay = " ".join(
                row.get(col, "") for col in ("displayName", "aliases", "acronyms", "query") if col in row
            )
            ident, label, file = _row_id_and_label(domain, row)
            hay = f"{hay} {ident}".lower()
            if needle and needle in hay:
                matches.append({"domain": domain, "id": ident, "displayName": label, "file": file})
            if len(matches) >= RESOLVE_MATCH_CAP:
                break
    return {"query": name, "match_count": len(matches), "matches": matches}


def tool_read_note(domain: str, note: str) -> dict[str, Any]:
    """Return the raw text of an entity note (frontmatter + body), capped."""
    domain_dir = ENTITIES / domain
    candidate = domain_dir / note
    if not candidate.exists() and not note.endswith(".md"):
        candidate = domain_dir / f"{note}.md"
    if not candidate.exists():
        hits = glob.glob(str(domain_dir / "**" / os.path.basename(
            note if note.endswith(".md") else f"{note}.md")), recursive=True)
        if hits:
            candidate = Path(hits[0])
    if not candidate.exists():
        return {"domain": domain, "note": note, "found": False, "text": ""}
    text = candidate.read_text(encoding="utf-8")
    truncated = len(text) > NOTE_CHAR_CAP
    return {
        "domain": domain,
        "note": note,
        "found": True,
        "path": str(candidate.relative_to(ROOT)),
        "truncated": truncated,
        "text": text[:NOTE_CHAR_CAP],
    }


def tool_list_domain(domain: str, limit: int = ROSTER_ROW_CAP) -> dict[str, Any]:
    """Return a domain's catalog rows for roster/list-type queries."""
    columns, rows = parse_catalog(domain)
    limit = max(1, min(int(limit or ROSTER_ROW_CAP), ROSTER_ROW_CAP))
    return {
        "domain": domain,
        "total": len(rows),
        "returned": min(len(rows), limit),
        "columns": columns,
        "rows": rows[:limit],
    }


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tool_search_cache(question: str, limit: int = 8) -> dict[str, Any]:
    """Step 0 read: surface prior cache entries similar in wording to `question`.

    Token-overlap candidate ranking only — relevance and freshness are the
    caller's judgment (Step 0 items 2-3), made by reading each candidate note.
    """
    q_tokens = set(_TOKEN_RE.findall(question.lower()))
    _, rows = parse_catalog("search")
    scored = []
    for row in rows:
        cand = row.get("query", "")
        c_tokens = set(_TOKEN_RE.findall(cand.lower()))
        overlap = len(q_tokens & c_tokens)
        if overlap:
            ident, label, file = _row_id_and_label("search", row)
            scored.append((overlap, {
                "queryId": ident,
                "query": cand,
                "status": row.get("status", ""),
                "askedDate": row.get("askedDate", ""),
                "reuseCount": row.get("reuseCount", ""),
                "file": file,
            }))
    scored.sort(key=lambda t: t[0], reverse=True)
    return {"question": question, "candidates": [c for _, c in scored[:limit]]}


# --------------------------------------------------------------------------- #
# Deterministic fast-path context
# --------------------------------------------------------------------------- #
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
_FAST_STOP = {
    "a", "about", "all", "an", "and", "are", "as", "at", "be", "by",
    "did", "do", "for", "from", "give", "has", "have", "how", "in", "is",
    "it", "list", "me", "more", "of", "on", "say", "said", "so", "the",
    "there", "to", "was", "what", "who", "with", "your",
}


def _normal_tokens(value: str) -> list[str]:
    return _TOKEN_RE.findall(value.lower())


def _cell_values(value: str) -> list[str]:
    """Return scalar/list catalog-cell values without treating prose as YAML."""
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = yaml.safe_load(value)
        except yaml.YAMLError:
            parsed = None
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
    if "," in value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


def _term_in_question(term: str, question: str) -> bool:
    tokens = _normal_tokens(term.replace("-", " "))
    if not tokens:
        return False
    question_tokens = _normal_tokens(question)
    size = len(tokens)
    for index in range(len(question_tokens) - size + 1):
        if question_tokens[index:index + size] == tokens:
            if size == 1 and len(tokens[0]) <= 3:
                return bool(re.search(rf"\b{re.escape(term)}\b", question))
            return True
    return False


def _catalog_entity_matches(question: str) -> list[dict[str, str]]:
    """Resolve entity mentions present verbatim as names, aliases, acronyms, or IDs."""
    found: dict[tuple[str, str], dict[str, str]] = {}
    for domain in FAST_DOMAINS:
        _, rows = parse_catalog(domain)
        for row in rows:
            ident, label, file = _row_id_and_label(domain, row)
            terms: list[tuple[int, str]] = [(70, ident), (100, label)]
            for column, weight in (("aliases", 90), ("acronyms", 95)):
                for value in _cell_values(row.get(column, "")):
                    terms.append((weight, value))
            matches = [
                (weight + len(_normal_tokens(term)), term)
                for weight, term in terms
                if _term_in_question(term, question)
                and not (
                    len(_normal_tokens(term)) == 1
                    and _normal_tokens(term)[0] in _FAST_STOP
                )
            ]
            if not matches:
                continue
            score, matched = max(matches)
            found[(domain, ident)] = {
                "domain": domain,
                "id": ident,
                "displayName": label,
                "file": file or f"{ident}.md",
                "matched": matched,
                "score": str(score),
            }
    matches = sorted(
        found.values(),
        key=lambda item: (-int(item["score"]), -len(_normal_tokens(item["matched"])),
                          item["domain"], item["id"]),
    )
    non_topic_ids = {item["id"] for item in matches if item["domain"] != "topic"}
    matches = [
        item for item in matches
        if not (item["domain"] == "topic" and item["id"] in non_topic_ids)
    ]
    kept = []
    for item in matches:
        tokens = set(_normal_tokens(item["matched"]))
        if item["domain"] == "topic" and any(
            tokens < set(_normal_tokens(other["matched"]))
            for other in matches
            if other is not item
        ):
            continue
        kept.append(item)
    return kept[:8]


def _note_path(domain: str, note: str) -> Path | None:
    candidate = ENTITIES / domain / note
    if not candidate.exists() and not note.endswith(".md"):
        candidate = candidate.with_suffix(".md")
    if candidate.exists():
        return candidate
    name = os.path.basename(note if note.endswith(".md") else f"{note}.md")
    hits = sorted((ENTITIES / domain).glob(f"**/{name}"))
    return hits[0] if hits else None


def _parse_note(domain: str, note: str) -> dict[str, Any] | None:
    path = _note_path(domain, note)
    if path is None:
        return None
    text = path.read_text(encoding="utf-8")
    frontmatter: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                frontmatter = {}
            body = parts[2]
    sections: dict[str, str] = {}
    matches = list(re.finditer(r"^##\s+(.+?)\s*$", body, re.MULTILINE))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1).strip().lower()] = body[match.end():end].strip()
    return {
        "path": str(path.relative_to(ROOT)),
        "frontmatter": frontmatter,
        "sections": sections,
    }


def _section(note: dict[str, Any], *names: str) -> str:
    sections = note.get("sections", {})
    for name in names:
        if name.lower() in sections:
            return sections[name.lower()]
    return ""


def _coverage_links(note: dict[str, Any]) -> list[dict[str, str]]:
    coverage = _section(note, "Coverage")
    links = []
    for target, label in _WIKILINK_RE.findall(coverage):
        links.append({"target": target, "label": label or target})
    return links


def _rank_evidence(note: dict[str, Any], question: str,
                   shared_targets: set[str] | None = None,
                   limit: int = FAST_EVIDENCE_CAP) -> list[dict[str, str]]:
    """Rank compact Coverage evidence against the question and compiled synthesis."""
    synthesis = " ".join(
        _section(note, heading)
        for heading in ("Summary", "Definition", "Office")
    )
    signal = {
        token for token in _normal_tokens(f"{question} {synthesis}")
        if token not in _FAST_STOP and len(token) > 2
    }
    shared_targets = shared_targets or set()
    ranked = []
    for order, link in enumerate(_coverage_links(note)):
        link_tokens = set(_normal_tokens(f"{link['target']} {link['label']}"))
        overlap = len(signal & link_tokens)
        score = overlap * 10
        if link["target"] in shared_targets:
            score += 1000
        if not link["target"].startswith("article/"):
            score += 20
        if link["label"].lower() == "coverage":
            score -= 2
        ranked.append((score, -order, link))
    ranked.sort(key=lambda item: (-item[0], -item[1]))
    return [link for _, _, link in ranked[:limit]]


def _identity_source_hint(note: dict[str, Any]) -> list[dict[str, str]]:
    """Select the strongest source hint for the first specific profile fact."""
    summary = _section(note, "Summary")
    sentences = re.split(r"(?<=[.!?])\s+", summary)
    specific = next(
        (sentence for sentence in sentences[1:] if re.search(
            r"\b(?:on|in)\s+(?:\d{1,2}\s+)?(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d{4})\b",
            sentence,
            re.IGNORECASE,
        )),
        sentences[1] if len(sentences) > 1 else summary,
    )
    signal = {
        token for token in _normal_tokens(specific)
        if token not in _FAST_STOP and len(token) > 2
    }
    ranked = []
    for order, link in enumerate(_coverage_links(note)):
        if link["target"].startswith("article/"):
            continue
        overlap = signal & set(_normal_tokens(
            f"{link['target']} {link['label']}"
        ))
        if overlap:
            ranked.append((len(overlap), -order, link))
    ranked.sort(reverse=True)
    return [ranked[0][2]] if ranked else []


def _coverage_anchor_block(note: dict[str, Any]) -> list[dict[str, str]]:
    """Recover the concise source block used to compile an entity synthesis."""
    links = _coverage_links(note)
    numbered: list[tuple[int, int, dict[str, str]]] = []
    for order, link in enumerate(links):
        if link["target"].startswith("article/"):
            break
        match = re.match(r"(\d+)", link["target"])
        if match:
            numbered.append((order, int(match.group(1)), link))
    drops = [
        (current / previous, order)
        for (_, previous, _), (order, current, _) in zip(numbered, numbered[1:])
        if previous > 0 and current < previous
    ]
    if not drops:
        return []
    ratio, start_order = min(drops)
    if ratio >= 0.8:
        return []

    generic = {
        "southeast-asia", "navy", "animation", "animation", "coverage", "reported",
        "the", "and", "for", "with",
    }
    selected = []
    primaries: set[str] = set()
    for link in links[start_order:]:
        label = link["label"]
        if link["target"].startswith("article/") or label.lower() == "coverage":
            break
        if "..." in label:
            if len(selected) >= 3:
                break
            continue
        primary = next(
            (token for token in _normal_tokens(label)
             if token not in generic and len(token) > 2),
            "",
        )
        if primary and primary in primaries:
            continue
        if primary:
            primaries.add(primary)
        selected.append(link)
        if len(selected) >= FAST_EVIDENCE_CAP:
            break
    return selected


def _enrich_evidence(link: dict[str, str]) -> dict[str, Any]:
    """Attach only the relevant compiled article sections to an evidence link."""
    enriched: dict[str, Any] = dict(link)
    target = link["target"]
    note_name = target[len("article/"):] if target.startswith("article/") else target
    article = _parse_note("article", note_name)
    if article is None:
        return enriched
    frontmatter = article["frontmatter"]
    enriched["source_id"] = str(frontmatter.get("sourceId") or "").strip()
    enriched["published_date"] = str(frontmatter.get("publishedDate") or "").strip()
    enriched["summary"] = _section(article, "Summary")[:600]
    enriched["key_points"] = _section(article, "Key Points")[:700]
    return enriched


def _query_shape(question: str, matches: list[dict[str, str]]) -> str:
    lowered = question.lower()
    if re.search(r"\b(list|all|every)\b", lowered):
        return "roster"
    if re.search(r"\b(current|who (?:is|was))\b", lowered) and any(
        item["domain"] == "appointments" for item in matches
    ):
        return "appointment"
    if re.search(r"\b(related|relationship|connection|connected)\b", lowered):
        return "relationship"
    if re.search(r"\b(no|none|any)\b", lowered) and re.search(
        r"\b(issue|incident|breach|attack)\b", lowered
    ):
        return "existence"
    if re.search(r"\b(who is|what is)\b", lowered):
        return "identity"
    if re.search(r"\b(say|said|talk|tell me more|what happened)\b", lowered):
        return "coverage"
    return "entity"


def _fast_matches_supported(shape: str, matches: list[dict[str, str]]) -> bool:
    """Reject ambiguous or unsupported resolution before a fast-path API call."""
    if shape == "relationship":
        return len(matches) == 2 and all(
            item["domain"] not in {"topic", "issues"} for item in matches
        )
    if shape == "coverage" and any(
        item["domain"] == "appointments" for item in matches
    ):
        return sum(item["domain"] == "appointments" for item in matches) == 1
    if shape == "existence":
        return len(matches) == 1
    if shape in {"identity", "coverage", "entity"}:
        return len(matches) == 1
    return False


def _subsection(text: str, heading: str) -> str:
    match = re.search(
        rf"^###\s+{re.escape(heading)}(?:\s+\(\d+\))?\s*$\n"
        rf"(.*?)(?=^###\s+|\Z)",
        text, re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    return match.group(1).strip() if match else ""


def _direct_roster_answer(question: str, matches: list[dict[str, str]]) -> dict[str, Any] | None:
    lowered = question.lower()
    domain_words = {
        "people": ("people", "person"),
        "organisations": ("organisations", "organizations", "organisation", "organization"),
        "outlet": ("outlets", "outlet"),
        "place": ("places", "place"),
        "country": ("countries", "country"),
        "topic": ("topics", "topic"),
    }
    requested = next(
        (domain for domain, words in domain_words.items() if any(
            re.search(rf"\b{re.escape(word)}\b", lowered) for word in words
        )),
        None,
    )
    if requested is None:
        return None
    anchors = [item for item in matches if item["domain"] != requested]
    if len(anchors) != 1:
        return None
    anchor = anchors[0]
    note = _parse_note(anchor["domain"], anchor["file"])
    if note is None:
        return None
    related = _section(note, "Related Entities", "Related entities")
    heading = {
        "people": "People", "organisations": "Organisations", "outlet": "Outlets",
        "place": "Places", "country": "Countries", "topic": "Topics",
    }[requested]
    links = _WIKILINK_RE.findall(_subsection(related, heading))
    if not links:
        return None
    labels = [label or target.replace("-", " ").title() for target, label in links]
    answer = (
        f"{len(labels)} {heading.lower()} are directly related to "
        f"{anchor['displayName']}:\n\n"
        + "\n".join(f"{index}. {label}" for index, label in enumerate(labels, 1))
        + "\n\nThis is a relationship-based roster rather than an article-by-article citation list."
    )
    return {
        "answer": answer,
        "entities_resolved": [anchor["id"]],
        "sources_cited": [],
        "status": "answered",
        "time_sensitive": False,
        "animation-industry": any(term in lowered for term in ("animation-industry", "studio-sector", "animation", "animation")),
        "reused_query_id": None,
    }


def _direct_appointment_answer(question: str,
                               matches: list[dict[str, str]]) -> dict[str, Any] | None:
    appointment_matches = [item for item in matches if item["domain"] == "appointments"]
    if len(appointment_matches) != 1 or not re.search(
        r"\b(current|who is)\b", question, re.IGNORECASE
    ):
        return None
    appointment = appointment_matches[0]
    note = _parse_note("appointments", appointment["file"])
    if note is None:
        return None
    holder_id = str(note["frontmatter"].get("currentHolder") or "").strip()
    if not holder_id:
        return None
    holders = _section(note, "Holders")
    holder_label = holder_id.replace("-", " ").title()
    for target, label in _WIKILINK_RE.findall(holders):
        if target == holder_id:
            holder_label = label or holder_label
            break
    office = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", _section(note, "Office"))
    office = re.sub(r"\[\[([^\]]+)\]\]", r"\1", office).strip()
    answer = f"{holder_label} is the current {appointment['displayName']}."
    if office:
        answer += f" {office}"
    return {
        "answer": answer,
        "entities_resolved": [appointment["id"], holder_id],
        "sources_cited": [],
        "status": "answered",
        "time_sensitive": True,
        "animation-industry": True,
        "reused_query_id": None,
    }


def _compact_entity_context(item: dict[str, str], note: dict[str, Any],
                            question: str, shared_targets: set[str]) -> dict[str, Any]:
    frontmatter = note["frontmatter"]
    keep_fields = (
        "displayName", "role", "affiliation", "country", "orgType", "category",
        "currentHolder", "articleCount", "mentionCount", "status", "crawlStatus",
        "crawlStatusAt", "lastCrawledAt", "ramification",
    )
    shape = _query_shape(question, [item])
    if shape == "identity":
        evidence = _identity_source_hint(note)
        evidence_scope = "summary_source_hint"
    elif shape == "coverage":
        anchors = _coverage_anchor_block(note)
        evidence = anchors or _rank_evidence(
            note, question, shared_targets, limit=6
        )
        evidence_scope = (
            "curated_synthesis_anchors" if anchors else "ranked_subset"
        )
    elif shape == "existence":
        evidence = _rank_evidence(
            note, question, shared_targets, limit=6
        )
        evidence_scope = "ranked_subset"
    else:
        evidence = _rank_evidence(note, question, shared_targets)
        evidence_scope = "ranked_subset"
    if shape in {"identity", "coverage", "existence"}:
        evidence = [_enrich_evidence(link) for link in evidence]
    related_cap = 1000 if shape == "existence" else 3500
    summary_cap = 2500 if shape == "existence" else 5000
    context = {
        "domain": item["domain"],
        "id": item["id"],
        "displayName": item["displayName"],
        "metadata": {key: frontmatter.get(key) for key in keep_fields if frontmatter.get(key) is not None},
        "summary": _section(note, "Summary", "Definition", "Office")[:summary_cap],
        "holders": _section(note, "Holders")[:2500],
        "related_entities": _section(
            note, "Related Entities", "Related entities"
        )[:related_cap],
        "coverage_evidence": evidence,
        "evidence_scope": evidence_scope,
    }
    return context


def _issue_catalog_matches(question: str) -> list[dict[str, Any]]:
    """Return filed issues whose names, aliases, or cluster tags match the query."""
    generic = {"breach", "incident", "issue", "attack", "security", "wiki"}
    question_tokens = {
        token for token in _normal_tokens(question)
        if token not in _FAST_STOP and token not in generic and len(token) > 3
    }
    _, rows = parse_catalog("issues")
    matches = []
    for row in rows:
        ident, label, _ = _row_id_and_label("issues", row)
        values = " ".join(
            str(row.get(key, ""))
            for key in ("displayName", "aliases", "clusterTags")
        )
        overlap = question_tokens & set(_normal_tokens(values))
        if overlap:
            matches.append({
                "id": ident,
                "displayName": label,
                "matched_tokens": sorted(overlap),
            })
    return matches


def _existence_expansions(
    question: str,
) -> list[tuple[dict[str, str], dict[str, Any]]]:
    """Find the small organisation graph most relevant to an existence check."""
    generic = {
        "breach", "incident", "issue", "attack", "security", "there", "wiki",
    }
    anchor_tokens = {
        token for token in _normal_tokens(question)
        if token not in _FAST_STOP and token not in generic and len(token) > 3
    }
    if not anchor_tokens:
        return []

    _, rows = parse_catalog("organisations")
    rows_by_id: dict[str, dict[str, str]] = {}
    seed_rows: list[dict[str, str]] = []
    for row in rows:
        ident, label, _ = _row_id_and_label("organisations", row)
        rows_by_id[ident] = row
        name_tokens = set(_normal_tokens(
            " ".join((ident, label, str(row.get("aliases", ""))))
        ))
        if anchor_tokens & name_tokens:
            seed_rows.append(row)

    selected: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
    for row in seed_rows:
        ident, label, file = _row_id_and_label("organisations", row)
        note = _parse_note("organisations", file or ident)
        if note is None:
            continue
        item = {
            "domain": "organisations",
            "id": ident,
            "displayName": label,
            "file": file or f"{ident}.md",
            "matched": ident,
            "score": "0",
        }
        selected[ident] = (item, note)

    # Follow only strongly shared related organisations, avoiding a broad graph walk.
    related_candidates: dict[str, tuple[int, dict[str, str], dict[str, Any]]] = {}
    for _, seed_note in selected.values():
        seed_coverage = {link["target"] for link in _coverage_links(seed_note)}
        related_ids = {
            target for target, _ in _WIKILINK_RE.findall(
                _section(seed_note, "Related Entities", "Related entities")
            )
            if target in rows_by_id
        }
        for ident in related_ids:
            row = rows_by_id[ident]
            _, label, file = _row_id_and_label("organisations", row)
            note = _parse_note("organisations", file or ident)
            if note is None:
                continue
            shared_count = len(
                seed_coverage & {link["target"] for link in _coverage_links(note)}
            )
            item = {
                "domain": "organisations",
                "id": ident,
                "displayName": label,
                "file": file or f"{ident}.md",
                "matched": ident,
                "score": "0",
            }
            previous = related_candidates.get(ident)
            if previous is None or shared_count > previous[0]:
                related_candidates[ident] = (shared_count, item, note)
    if related_candidates:
        strongest = max(shared for shared, _, _ in related_candidates.values())
        threshold = max(3, round(strongest * 0.15))
        for ident, (shared, item, note) in related_candidates.items():
            if shared >= threshold:
                selected.setdefault(ident, (item, note))
    return [selected[ident] for ident in sorted(selected)]


def build_fast_context(question: str) -> dict[str, Any] | None:
    """Build a bounded, source-backed context packet for one-call query answering."""
    matches = _catalog_entity_matches(question)
    shape = _query_shape(question, matches)
    # An existence check ("is there any X in the wiki?") legitimately resolves no
    # catalog entity — answering it is precisely the job of _existence_expansions
    # below, which discovers the relevant organisation graph from the question's
    # anchor tokens. Bailing out here on an empty match list silently disabled
    # that path once the topic consolidation retired the broad topic notes that
    # used to supply an incidental seed match.
    if not matches and shape != "existence":
        return None
    notes: list[tuple[dict[str, str], dict[str, Any]]] = []
    for item in matches:
        note = _parse_note(item["domain"], item["file"])
        if note is not None:
            notes.append((item, note))
    if not notes and shape != "existence":
        return None

    # Appointment questions about remarks need both dated holders' person notes.
    if shape == "coverage":
        if any(item["domain"] == "appointments" for item, _ in notes):
            notes = [
                (item, note) for item, note in notes
                if item["domain"] != "topic"
            ]
        extra_ids: set[str] = set()
        for item, note in notes:
            if item["domain"] == "appointments":
                extra_ids.update(target for target, _ in _WIKILINK_RE.findall(
                    _section(note, "Holders")
                ))
        _, people_rows = parse_catalog("people")
        people_by_id = {
            _row_id_and_label("people", row)[0]: row for row in people_rows
        }
        for ident in sorted(extra_ids):
            row = people_by_id.get(ident)
            if row is None:
                continue
            _, label, file = _row_id_and_label("people", row)
            item = {"domain": "people", "id": ident, "displayName": label,
                    "file": file or f"{ident}.md", "matched": ident, "score": "0"}
            note = _parse_note("people", item["file"])
            if note is not None:
                notes.append((item, note))

    if shape == "existence":
        known = {(item["domain"], item["id"]) for item, _ in notes}
        for item, note in _existence_expansions(question):
            if (item["domain"], item["id"]) not in known:
                notes.append((item, note))
                known.add((item["domain"], item["id"]))
        if not notes:
            return None

    coverage_sets = [
        {link["target"] for link in _coverage_links(note)}
        for _, note in notes
        if _coverage_links(note)
    ]
    existence_shared: list[str] = []
    if shape == "existence":
        counts = Counter(
            target
            for item, note in notes
            if item["domain"] == "organisations"
            for target in {link["target"] for link in _coverage_links(note)}
        )
        existence_shared = [
            target for target, count in sorted(
                counts.items(),
                key=lambda pair: (
                    pair[0].startswith("article/"), -pair[1], pair[0],
                ),
            )
            if count >= 2
        ][:6]
        shared_targets = set(existence_shared)
    else:
        shared_targets = (
            set.intersection(*coverage_sets) if len(coverage_sets) >= 2 else set()
        )
    resolved_notes = (
        [(item, note) for item, note in notes if item["domain"] != "topic"]
        if shape == "existence"
        else notes
    )
    entity_contexts = [
        _compact_entity_context(item, note, question, shared_targets)
        for item, note in notes
    ]
    shared_evidence: list[dict[str, Any]] = []
    if shape == "existence":
        labels = {
            link["target"]: link["label"]
            for _, note in notes
            for link in _coverage_links(note)
        }
        shared_evidence = [
            _enrich_evidence({"target": target, "label": labels.get(target, target)})
            for target in existence_shared
        ]
        for entity in entity_contexts:
            entity["coverage_evidence"] = []
    packet = {
        "query_shape": shape,
        "filed_issue_matches": (
            _issue_catalog_matches(question) if shape == "existence" else []
        ),
        "resolved_entities": [
            {"domain": item["domain"], "id": item["id"], "displayName": item["displayName"]}
            for item, _ in resolved_notes
        ],
        "shared_coverage": (
            existence_shared if shape == "existence"
            else sorted(shared_targets)[:20]
        ),
        "shared_evidence": shared_evidence,
        "entities": entity_contexts,
    }
    encoded = json.dumps(packet, ensure_ascii=False)
    if len(encoded) > FAST_CONTEXT_CHAR_CAP:
        for entity in packet["entities"]:
            entity["related_entities"] = entity["related_entities"][:1000]
            entity["summary"] = entity["summary"][:3000]
            entity["coverage_evidence"] = entity["coverage_evidence"][:8]
            for evidence in entity["coverage_evidence"]:
                if "summary" in evidence:
                    evidence["summary"] = evidence["summary"][:350]
                evidence.pop("key_points", None)
        encoded = json.dumps(packet, ensure_ascii=False)
    return packet if len(encoded) <= FAST_CONTEXT_CHAR_CAP else None


# --------------------------------------------------------------------------- #
# Cache write-back (Steps 6-7) — deterministic, gated by cache_write
# --------------------------------------------------------------------------- #
def _slugify(text: str, max_words: int = 7) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:max_words]) or "query"


def _procedure_version() -> str:
    try:
        fm = yaml.safe_load(PROCEDURE_PATH.read_text(encoding="utf-8").split("---", 2)[1]) or {}
        val = fm.get("last_updated", "")
        return val.isoformat() if hasattr(val, "isoformat") and not isinstance(val, str) else str(val)
    except Exception:
        return ""


def _regenerate_search_catalog() -> str | None:
    try:
        subprocess.run(
            [sys.executable, str(SCRIPTS / "generate_catalog.py"), "search"],
            cwd=str(ROOT), check=True, capture_output=True, timeout=90,
        )
        return None
    except Exception as exc:  # pragma: no cover - best-effort
        return f"catalog regen failed: {exc}"


def _wikilink_lines(items: list[Any]) -> str:
    out = []
    for it in items or []:
        if isinstance(it, dict):
            ident = it.get("id") or it.get("queryId") or ""
            label = it.get("displayName") or it.get("label")
        else:
            ident, label = str(it), None
        ident = str(ident).split("/")[-1].strip()
        if not ident:
            continue
        out.append(f"- [[{ident}|{label}]]" if label else f"- [[{ident}]]")
    return "\n".join(out) if out else "<!-- none -->"


def persist_answer(question: str, final: dict[str, Any]) -> dict[str, Any]:
    """Steps 6-7: file a new query note, append the log, regenerate the catalog."""
    now = datetime.now(timezone.utc).astimezone()
    query_id = f"{_slugify(question)}-{hashlib.sha1(question.strip().encode('utf-8')).hexdigest()[:8]}"
    animation_industry = bool(final.get("animation-industry"))
    status = final.get("status", "answered")
    note = f"""---
queryId: {query_id}
query: {json.dumps(question, ensure_ascii=False)}
askedDate: {now.strftime('%Y-%m-%dT%H:%M:%S')}
status: {status}
reuseCount: 0
timeSensitive: {str(bool(final.get('time_sensitive'))).lower()}
procedureVersion: {_procedure_version()}
tags: {"['#animation-industry']" if animation_industry else "[]"}
---

## Question
{question}

## Answer
{final.get('answer', '').strip()}

## Entities Resolved
{_wikilink_lines(final.get('entities_resolved'))}

## Sources Cited
{_wikilink_lines(final.get('sources_cited'))}

## AI Context
Filed by `scripts/query.py` per Step 7.
{'`#animation-industry` — touches deny-listed material; route any export through the sanitized derived copy per §10.' if animation-industry else 'Not `#animation-industry`-flagged.'}
"""
    SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    note_path = SEARCH_DIR / f"{query_id}.md"
    note_path.write_text(note, encoding="utf-8")

    log_line = (
        f"- {now.strftime('%Y-%m-%dT%H:%M:%S%z')} | source: user query "
        f"\"{question}\" | entity: [[{query_id}]] | action: created — filed by "
        f"scripts/query.py per Step 7 | reasoning: full Steps 0-7 run via query.py; "
        f"cache_write on.\n"
    )
    with (SEARCH_DIR / "log.md").open("a", encoding="utf-8") as fh:
        fh.write(log_line)

    warn = _regenerate_search_catalog()
    _CATALOG_CACHE.pop("search", None)
    return {"query_id": query_id, "path": str(note_path.relative_to(ROOT)), "warning": warn}


def register_reuse(matched_query_id: str, question: str) -> dict[str, Any]:
    """Step 0 item 4: a cache hit — bump reuseCount on the matched entry + log."""
    now = datetime.now(timezone.utc).astimezone()
    note_path = SEARCH_DIR / f"{matched_query_id}.md"
    old = new = None
    if note_path.exists():
        text = note_path.read_text(encoding="utf-8")

        def _bump(m: "re.Match[str]") -> str:
            nonlocal old, new
            old = int(m.group(1))
            new = old + 1
            return f"reuseCount: {new}"

        text = re.sub(r"reuseCount:\s*(\d+)", _bump, text, count=1)
        note_path.write_text(text, encoding="utf-8")
    log_line = (
        f"- {now.strftime('%Y-%m-%dT%H:%M:%S%z')} | source: user query "
        f"\"{question}\" | entity: [[{matched_query_id}]] | action: cache hit — "
        f"returned ## Answer verbatim, incremented reuseCount {old} -> {new} | "
        f"reasoning: Steps 1-7 did not run, per Step 0 item 4.\n"
    )
    with (SEARCH_DIR / "log.md").open("a", encoding="utf-8") as fh:
        fh.write(log_line)
    warn = _regenerate_search_catalog()
    _CATALOG_CACHE.pop("search", None)
    return {"query_id": matched_query_id, "reuseCount": new, "warning": warn}


# --------------------------------------------------------------------------- #
# Model tool schemas + dispatch (API mode)
# --------------------------------------------------------------------------- #
def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def build_tools(cache_read: bool) -> list[dict]:
    tools = [
        _fn("resolve_entity",
            "Resolve a candidate entity name against domain catalog.md files "
            "(displayName / aliases / acronyms / id). Returns matching records; "
            "you disambiguate. Use this instead of grepping the article corpus.",
            {"name": {"type": "string"},
             "domains": {"type": "array", "items": {"type": "string"},
                         "description": "optional subset of domains to search"}},
            ["name"]),
        _fn("read_note",
            "Read one entity note's full text (frontmatter + body: Summary, "
            "Coverage, Related Entities, Holders, etc.).",
            {"domain": {"type": "string"},
             "note": {"type": "string", "description": "note id or catalog File value"}},
            ["domain", "note"]),
        _fn("list_domain",
            "Return a domain's catalog rows for roster / list-type queries. "
            "Do not open every note; build the roster from these columns.",
            {"domain": {"type": "string"},
             "limit": {"type": "integer"}},
            ["domain"]),
        _fn("submit_answer",
            "Deliver the final answer and end the query. `answer` must be a "
            "natural, self-contained reply with NO references to the wiki/corpus "
            "and NO appended citations (Step 5 item 21). Put resolved entities "
            "and cited sources in their own fields, not in `answer`.",
            {"answer": {"type": "string"},
             "entities_resolved": {"type": "array", "items": {"type": "string"},
                                   "description": "domain/id of each entity opened"},
             "sources_cited": {"type": "array", "items": {"type": "string"},
                               "description": "article id/slug of each source used"},
             "status": {"type": "string", "enum": ["answered", "unresolved"]},
             "time_sensitive": {"type": "boolean",
                                "description": "true if the question is framed relative to 'now'"},
             "animation-industry": {"type": "boolean", "description": "true if it touches Animation Industry/Studio Sector/Animation Technology material"},
             "reused_query_id": {"type": ["string", "null"],
                                 "description": "set to a cached queryId if this reuses a fresh cache hit"}},
            ["answer", "entities_resolved", "sources_cited", "status", "time_sensitive", "animation-industry"]),
    ]
    if cache_read:
        tools.insert(0, _fn(
            "search_cache",
            "Step 0: check the search cache for prior questions similar to this "
            "one. Returns candidates; judge relevance and freshness yourself by "
            "reading the candidate note and re-checking entity counts.",
            {"question": {"type": "string"}, "limit": {"type": "integer"}},
            ["question"]))
    return tools


def dispatch_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "resolve_entity":
        return tool_resolve_entity(args["name"], args.get("domains"))
    if name == "read_note":
        return tool_read_note(args["domain"], args["note"])
    if name == "list_domain":
        return tool_list_domain(args["domain"], args.get("limit", ROSTER_ROW_CAP))
    if name == "search_cache":
        return tool_search_cache(args["question"], args.get("limit", 8))
    return {"error": f"unknown tool {name}"}


# --------------------------------------------------------------------------- #
# Model call (OpenAI Responses API over stdlib urllib) + agent loop
# --------------------------------------------------------------------------- #
def _runtime_preamble(cache_read: bool, cache_write: bool) -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    return (
        "You are executing the Entity Query Procedure below headlessly, called "
        "by an automated workflow — there is no human to ask follow-ups, so "
        "resolve ambiguity yourself and answer as best you can.\n\n"
        f"Today's date: {today}. You operate on the live vault via the provided "
        "tools; never invent facts or use outside knowledge (Step 5 item 20).\n\n"
        f"cache_read is {'ON' if cache_read else 'OFF'}: "
        + ("run Step 0 first (call search_cache, then judge relevance/freshness).\n"
           if cache_read else
           "SKIP Step 0 entirely; do not look for a cached answer. Start at Step 1.\n")
        + f"cache_write is {'ON' if cache_write else 'OFF'}: "
        + ("the system will persist your answer to entities/search/ after you "
           "finish (Steps 6-7 are handled for you in Python).\n"
           if cache_write else
           "your answer will NOT be persisted; still answer normally.\n")
        + "\nFinish EVERY query by calling submit_answer exactly once. If you "
        "reused a fresh cache hit, set reused_query_id to that cached queryId and "
        "copy its answer verbatim into `answer`.\n\n"
        "=== BEGIN query_procedure.md ===\n"
    )


def _call_responses(api_key: str, model: str, instructions: str,
                    input_items: list[dict], tools: list[dict],
                    reasoning_effort: str = "medium",
                    tool_choice: Any = "auto") -> dict:
    payload = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "tools": tools,
        "tool_choice": tool_choice,
        "reasoning": {"effort": reasoning_effort},
        "store": False,
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(2000).decode("utf-8", errors="replace")
        raise QueryError(f"OpenAI API returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise QueryError(f"OpenAI API request failed: {exc}") from exc


def _message_text(output_items: list[dict]) -> str:
    parts = []
    for item in output_items:
        if item.get("type") == "message":
            for chunk in item.get("content", []):
                if chunk.get("type") in {"output_text", "text"}:
                    parts.append(chunk.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _submit_answer_tool() -> dict[str, Any]:
    return next(tool for tool in build_tools(False) if tool["name"] == "submit_answer")


def _fast_instructions() -> str:
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    return (
        "Answer one ai-animation entity question from the supplied local evidence packet. "
        f"Today's date is {today}. Use only the packet; never use outside knowledge or invent facts. "
        "Treat compiled summaries as synthesis backed by their Coverage evidence. For relationship "
        "questions, distinguish shared mentions from direct interaction. For negative questions, "
        "distinguish no filed issue from no underlying coverage. For an existence question, treat "
        "filed_issue_matches as the authoritative filed-issue check and shared_coverage as the "
        "strongest cross-entity evidence; explain the relevant response, deployment, and coverage "
        "pattern supported by the entity summaries without inventing recurrence or escalation. "
        "For an identity question, give a "
        "concise but complete profile from the entire supplied summary: include concurrent roles, "
        "representative named examples, work areas, and operating or rhetorical style when present, "
        "rather than merely restating the first sentence. For a coverage question whose entity has "
        "evidence_scope `curated_synthesis_anchors`, synthesize every distinct supplied item—such "
        "as leadership, people, modernisation, industry, operations, and heritage—unless it is a "
        "syndicated duplicate. For `ranked_subset`, use only items that directly answer the question "
        "and omit incidental mentions or sources for which the packet gives no relevant substance. "
        "Answer naturally and directly: "
        "do not mention a wiki, corpus, catalog, cache, procedure, filenames, or internal mechanics, "
        "and do not append citations to the answer text. In entities_resolved use the bare `id` "
        "values from resolved_entities, without domain prefixes. In sources_cited copy the exact "
        "Coverage target values that underpin the answer. Set time_sensitive true only when the "
        "question itself uses a relative/current framing such as current, now, latest, or recent. "
        "Preserve uncertainty and return status unresolved if the packet cannot support the "
        "requested claim. Call submit_answer exactly once."
    )


def _run_fast_model(api_key: str, model: str, question: str,
                    context: dict[str, Any]) -> dict[str, Any] | None:
    """Complete a supported query with one forced structured model response."""
    tool = _submit_answer_tool()
    input_items = [{
        "role": "user",
        "content": (
            f"Question:\n{question}\n\n"
            "Local evidence packet (JSON):\n"
            + json.dumps(context, ensure_ascii=False)
        ),
    }]
    response = _call_responses(
        api_key,
        model,
        _fast_instructions(),
        input_items,
        [tool],
        reasoning_effort="low",
        tool_choice={"type": "function", "name": "submit_answer"},
    )
    calls = [
        item for item in response.get("output", [])
        if item.get("type") == "function_call" and item.get("name") == "submit_answer"
    ]
    if len(calls) != 1:
        return None
    try:
        final = json.loads(calls[0].get("arguments") or "{}")
    except json.JSONDecodeError:
        return None
    if not (final.get("answer") or "").strip() or final.get("status") == "unresolved":
        return None
    # Resolution and source membership are deterministic; never accept model-added IDs.
    final["entities_resolved"] = [
        item["id"] for item in context.get("resolved_entities", [])
    ]
    allowed_sources: dict[str, str] = {}
    source_targets = list(context.get("shared_coverage", []))
    source_targets.extend(
        item.get("target", "") for item in context.get("shared_evidence", [])
    )
    for entity in context.get("entities", []):
        evidence_items = entity.get("coverage_evidence", [])
        canonical = [
            item for item in evidence_items
            if not item.get("target", "").startswith("article/")
        ]
        for item in evidence_items:
            target = item.get("target", "")
            source_targets.append(target)
            if not target.startswith("article/") or not canonical:
                continue
            tokens = {
                token for token in _normal_tokens(item.get("label", ""))
                if token not in _FAST_STOP and len(token) > 2
            }
            best: tuple[float, str] | None = None
            for candidate in canonical:
                candidate_tokens = {
                    token for token in _normal_tokens(candidate.get("label", ""))
                    if token not in _FAST_STOP and len(token) > 2
                }
                if not tokens or not candidate_tokens:
                    continue
                score = len(tokens & candidate_tokens) / min(
                    len(tokens), len(candidate_tokens)
                )
                if best is None or score > best[0]:
                    best = (score, candidate["target"])
            if best and best[0] >= 0.6:
                basename = best[1].rstrip("/").split("/")[-1]
                match = re.match(r"(\d+)", basename)
                canonical_id = match.group(1) if match else basename
                target_basename = target.rstrip("/").split("/")[-1]
                target_match = re.match(r"(\d+)", target_basename)
                target_id = (
                    target_match.group(1) if target_match else target_basename
                )
                for alias in (target, target_basename, target_id):
                    allowed_sources[alias] = canonical_id
    for target in source_targets:
        basename = target.rstrip("/").split("/")[-1]
        source_id_match = re.match(r"(\d+)", basename)
        source_id = source_id_match.group(1) if source_id_match else basename
        for alias in (target, basename, source_id):
            allowed_sources.setdefault(alias, source_id)
    normalized_sources = []
    for source in final.get("sources_cited", []):
        normalized = allowed_sources.get(str(source))
        if normalized and normalized not in normalized_sources:
            normalized_sources.append(normalized)
    final["sources_cited"] = normalized_sources
    final.setdefault("status", "answered")
    final.setdefault("time_sensitive", False)
    final.setdefault("animation-industry", False)
    final.setdefault("reused_query_id", None)
    return final


def _run_legacy_model(api_key: str, model: str, question: str,
                      cache_read: bool, cache_write: bool) -> dict[str, Any]:
    """Run the original procedure-driven multi-turn tool loop unchanged."""
    instructions = (
        _runtime_preamble(cache_read, cache_write)
        + PROCEDURE_PATH.read_text(encoding="utf-8")
    )
    tools = build_tools(cache_read)
    input_items: list[dict] = [{"role": "user", "content": question}]
    final: dict[str, Any] | None = None

    for _ in range(MAX_TOOL_TURNS):
        resp = _call_responses(api_key, model, instructions, input_items, tools)
        output_items = resp.get("output", [])
        calls = [item for item in output_items if item.get("type") == "function_call"]
        if not calls:
            final = {
                "answer": _message_text(output_items),
                "entities_resolved": [],
                "sources_cited": [],
                "status": "answered",
                "time_sensitive": False,
                "animation-industry": False,
                "reused_query_id": None,
            }
            break
        for call in calls:
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            input_items.append({
                "type": "function_call",
                "call_id": call["call_id"],
                "name": call["name"],
                "arguments": call.get("arguments", "{}"),
            })
            if call["name"] == "submit_answer":
                final = args
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps({"received": True}),
                })
            else:
                result = dispatch_tool(call["name"], args)
                input_items.append({
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": json.dumps(result, ensure_ascii=False),
                })
        if final is not None:
            break

    if final is None:
        raise QueryError(f"no answer after {MAX_TOOL_TURNS} tool turns")
    return final


def run_query(question: str, cache_read: bool | None = None,
              cache_write: bool | None = None, model: str | None = None) -> dict[str, Any]:
    """API mode: run one question autonomously via the model, return a result."""
    load_local_env()
    if not PROCEDURE_PATH.exists():
        raise QueryError(f"missing procedure: {PROCEDURE_PATH}")

    read, write = resolve_flags(cache_read, cache_write)
    model = model or DEFAULT_MODEL

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise QueryError("OPENAI_API_KEY is not set (checked env and .env.local).")

    final: dict[str, Any] | None = None
    if fast_path_enabled() and not read:
        matches = _catalog_entity_matches(question)
        shape = _query_shape(question, matches)
        if shape == "roster":
            final = _direct_roster_answer(question, matches)
        elif shape == "appointment":
            final = _direct_appointment_answer(question, matches)
        if final is None and _fast_matches_supported(shape, matches):
            context = build_fast_context(question)
            if context is not None:
                final = _run_fast_model(api_key, model, question, context)
    if final is None:
        final = _run_legacy_model(api_key, model, question, read, write)

    result: dict[str, Any] = {
        "question": question,
        "answer": (final.get("answer") or "").strip(),
        "status": final.get("status", "answered"),
        "entities_resolved": final.get("entities_resolved", []),
        "sources_cited": final.get("sources_cited", []),
        "time_sensitive": bool(final.get("time_sensitive")),
        "animation-industry": bool(final.get("animation-industry")),
        "cache_read": read,
        "cache_write": write,
        "cache_hit": bool(final.get("reused_query_id")),
        "reused_query_id": final.get("reused_query_id"),
        "query_id": None,
        "cache_written": False,
    }

    if write:
        if result["cache_hit"] and result["reused_query_id"]:
            info = register_reuse(result["reused_query_id"], question)
            result["query_id"] = info["query_id"]
        else:
            info = persist_answer(question, final)
            result["query_id"] = info["query_id"]
        result["cache_written"] = True
    return result


# --------------------------------------------------------------------------- #
# HTTP server (stdlib) — the endpoint n8n calls (API mode)
# --------------------------------------------------------------------------- #
def serve(port: int = 8080) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict) -> None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/") == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if self.path.rstrip("/") != "/query":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"error": "invalid JSON body"})
                return
            question = (body.get("question") or "").strip()
            if not question:
                self._send(400, {"error": "missing 'question'"})
                return
            try:
                result = run_query(
                    question,
                    cache_read=body.get("cache_read"),
                    cache_write=body.get("cache_write"),
                    model=body.get("model"),
                )
                self._send(200, result)
            except QueryError as exc:
                self._send(502, {"error": str(exc)})
            except Exception as exc:  # pragma: no cover
                self._send(500, {"error": f"internal error: {exc}"})

        def log_message(self, *_args):  # silence default stderr logging
            return

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"query.py serving on http://0.0.0.0:{port}  (POST /query, GET /health)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
_SUBCOMMANDS = {"ask", "serve", "resolve", "read", "list", "search", "submit"}


def _print(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Query the ai-animation wiki.")
    sub = parser.add_subparsers(dest="cmd")

    # --- API mode (autonomous; uses the model) ---
    p_ask = sub.add_parser("ask", help="answer a question autonomously (uses the model / API key)")
    p_ask.add_argument("question")
    p_ask.add_argument("--model", default=None, help=f"model override (default {DEFAULT_MODEL})")
    p_ask.add_argument("--no-cache-read", dest="cache_read", action="store_false", default=None,
                       help="skip the search-cache short-circuit (Step 0)")
    p_ask.add_argument("--no-cache-write", dest="cache_write", action="store_false", default=None,
                       help="do not persist the Q&A to entities/search/ (Steps 6-7)")
    p_ask.add_argument("--json", action="store_true", help="print the full JSON result")

    p_serve = sub.add_parser("serve", help="run the HTTP endpoint for n8n")
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("QUERY_PORT", "8080")))

    # --- Claude-assisted primitives (no model; the agent drives these) ---
    p_res = sub.add_parser("resolve", help="resolve an entity name against catalogs")
    p_res.add_argument("name")
    p_res.add_argument("--domains", nargs="*", default=None,
                       help="optional subset of domains to search")

    p_read = sub.add_parser("read", help="read one entity note")
    p_read.add_argument("domain")
    p_read.add_argument("note")

    p_list = sub.add_parser("list", help="list a domain's catalog rows (roster queries)")
    p_list.add_argument("domain")
    p_list.add_argument("--limit", type=int, default=ROSTER_ROW_CAP)

    p_search = sub.add_parser("search", help="search the query cache (Step 0)")
    p_search.add_argument("question")

    p_submit = sub.add_parser("submit", help="write an answer to the cache (Steps 6-7)")
    p_submit.add_argument("--question", required=True)
    p_submit.add_argument("--answer", default="")
    p_submit.add_argument("--entities", nargs="*", default=[], help="domain/id of each entity opened")
    p_submit.add_argument("--sources", nargs="*", default=[], help="article id/slug of each source used")
    p_submit.add_argument("--status", default="answered", choices=["answered", "unresolved"])
    p_submit.add_argument("--animation-industry", action="store_true")
    p_submit.add_argument("--time-sensitive", dest="time_sensitive", action="store_true")
    p_submit.add_argument("--reuse", default=None, help="cached queryId to reuse (bumps reuseCount)")

    # Back-compat shorthand: `query.py "question"` -> `ask "question"`
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and not raw[0].startswith("-") and raw[0] not in _SUBCOMMANDS:
        raw = ["ask"] + raw
    args = parser.parse_args(raw)

    if args.cmd == "serve":
        serve(args.port)
        return 0
    if args.cmd == "resolve":
        _print(tool_resolve_entity(args.name, args.domains))
        return 0
    if args.cmd == "read":
        _print(tool_read_note(args.domain, args.note))
        return 0
    if args.cmd == "list":
        _print(tool_list_domain(args.domain, args.limit))
        return 0
    if args.cmd == "search":
        _print(tool_search_cache(args.question))
        return 0
    if args.cmd == "submit":
        load_local_env()
        if args.reuse:
            _print(register_reuse(args.reuse, args.question))
        else:
            final = {"answer": args.answer, "entities_resolved": args.entities,
                     "sources_cited": args.sources, "status": args.status,
                     "time_sensitive": args.time_sensitive, "animation-industry": args.animation-industry}
            _print(persist_answer(args.question, final))
        return 0
    if args.cmd == "ask":
        try:
            result = run_query(args.question, cache_read=args.cache_read,
                               cache_write=args.cache_write, model=args.model)
        except QueryError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print(result)
        else:
            print(result["answer"])
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
