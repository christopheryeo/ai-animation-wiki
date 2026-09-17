#!/usr/bin/env python3
"""Review missing person country fields, with optional internet confirmation.

Default mode is read-only and vault-grounded:
  python3 scripts/people_country_inference.py --limit 50 --format markdown

Optional internet confirmation uses Wikipedia's public API:
  python3 scripts/people_country_inference.py --internet-confirm --limit 50

The script produces a review table. It does not edit person notes; writing
approved updates remains a deliberate follow-up step per
people_country_inference_procedure.md.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import textwrap
import time
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PEOPLE_DIR = ROOT / "entities" / "people"
ARTICLE_DIR = ROOT / "entities" / "article"
DEFAULT_USER_AGENT = "ai-animation-country-inference/1.0"


COUNTRY_ALIASES = {
    "Australia": ["Australia", "Australian"],
    "Canada": ["Canada", "Canadian"],
    "Cambodia": ["Cambodia", "Cambodian"],
    "China": ["China", "Chinese"],
    "Croatia": ["Croatia", "Croatian"],
    "Denmark": ["Denmark", "Danish"],
    "Egypt": ["Egypt", "Egyptian"],
    "Finland": ["Finland", "Finnish"],
    "France": ["France", "French"],
    "Germany": ["Germany", "German"],
    "India": ["India", "Indian"],
    "Indonesia": ["Indonesia", "Indonesian"],
    "Iran": ["Iran", "Iranian"],
    "Israel": ["Israel", "Israeli"],
    "Italy": ["Italy", "Italian"],
    "Japan": ["Japan", "Japanese"],
    "Malaysia": ["Malaysia", "Malaysian"],
    "Myanmar": ["Myanmar", "Burmese"],
    "Netherlands": ["Netherlands", "Dutch"],
    "North Korea": ["North Korea", "North Korean"],
    "Norway": ["Norway", "Norwegian"],
    "Pakistan": ["Pakistan", "Pakistani"],
    "Philippines": ["Philippines", "Philippine", "Filipino"],
    "Russia": ["Russia", "Russian"],
    "Saudi Arabia": ["Saudi Arabia", "Saudi"],
    "Southeast Asia": ["Southeast Asia", "Singaporean"],
    "South Korea": ["South Korea", "South Korean", "S. Korea"],
    "Spain": ["Spain", "Spanish"],
    "Sweden": ["Sweden", "Swedish"],
    "Switzerland": ["Switzerland", "Swiss"],
    "Taiwan": ["Taiwan", "Taiwanese"],
    "Thailand": ["Thailand", "Thai"],
    "Turkey": ["Turkey", "Turkish", "Türkiye"],
    "Ukraine": ["Ukraine", "Ukrainian"],
    "United Arab Emirates": ["United Arab Emirates", "UAE", "Emirati"],
    "United Kingdom": ["United Kingdom", "UK", "U.K.", "British"],
    "United States": ["United States", "US", "U.S.", "American"],
    "Vietnam": ["Vietnam", "Vietnamese"],
}

TITLE_WORDS = (
    r"(?:President|Prime Minister|PM|Minister|Secretary|Chief Executive|Founder|"
    r"director|producer|showrunner|commissioner|leader|official|spokes(?:man|woman|person))"
)
TITLE_RE = re.compile(TITLE_WORDS, re.I)


@dataclass
class Person:
    person_id: str
    display_name: str
    role: str
    affiliation: str
    country: str
    mention_count: int


@dataclass
class Evidence:
    country: str = ""
    confidence: str = "none"
    evidence: str = ""
    source: str = ""
    source_url: str = ""


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def section(text: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return match.group(1).strip() if match else ""


def markdown_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def load_people(include_known: bool) -> list[Person]:
    rows: list[Person] = []
    for line in read_text(PEOPLE_DIR / "catalog.md").splitlines():
        if not line.startswith("|") or line.startswith("|---") or " personId " in line:
            continue
        parts = markdown_cells(line)
        if len(parts) < 8:
            continue
        person_id, display, role, affiliation, country, _aliases, count, _file = parts[:8]
        if country and not include_known:
            continue
        try:
            mention_count = int(count)
        except ValueError:
            mention_count = 0
        rows.append(Person(person_id, display, role, affiliation, country, mention_count))
    return sorted(rows, key=lambda p: (-p.mention_count, p.display_name.lower()))


def article_paths() -> dict[str, Path]:
    return {path.stem: path for path in ARTICLE_DIR.glob("*/*.md")}


def coverage_links(person_text: str) -> list[tuple[str, str]]:
    coverage = section(person_text, "Coverage")
    return re.findall(r"\[\[([^]|]+)(?:\|([^]]+))?\]\]", coverage)


def clean_snippet(text: str, width: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return textwrap.shorten(text, width=width, placeholder="...")


def country_in_direct_text(text: str, person_name: str) -> Evidence:
    surname = person_name.split()[-1]
    chunks = re.split(r"(?<=[.!?])\s+|\n|;", text)
    for chunk in chunks:
        sentence = clean_snippet(chunk, width=500)
        if not sentence:
            continue
        if person_name.lower() not in sentence.lower() and surname.lower() not in sentence.lower():
            continue
        for country, aliases in COUNTRY_ALIASES.items():
            for alias in aliases:
                alias_re = re.escape(alias)
                # The country adjective must attach to this person's own title.
                # Avoid matching "Indonesian President Prabowo ... Prime Minister
                # Lawrence Wong" as evidence that Lawrence Wong is Indonesian.
                before_name = rf"(?:[\w'()/-]+\s+){{0,7}}"
                patterns = [
                    rf"\b{alias_re}\b(?:'s)?\s+{TITLE_WORDS}\s+{before_name}\b{re.escape(person_name)}\b",
                    rf"\b{alias_re}\b(?:'s)?\s+{TITLE_WORDS}\s+{before_name}\b{re.escape(surname)}\b",
                    rf"\b{re.escape(person_name)}\b[^.]*?\b{TITLE_WORDS}\b[^.]*?\b(?:of|for|in)\s+\b{alias_re}\b",
                    rf"\b{re.escape(surname)}\b[^.]*?\b{TITLE_WORDS}\b[^.]*?\b(?:of|for|in)\s+\b{alias_re}\b",
                    rf"\b{re.escape(person_name)}\b[^.]*?\b(?:is|was|as)\s+(?:an?|the)?\s*\b{alias_re}\b\s+{TITLE_WORDS}",
                    rf"\b{re.escape(surname)}\b[^.]*?\b(?:is|was|as)\s+(?:an?|the)?\s*\b{alias_re}\b\s+{TITLE_WORDS}",
                ]
                if any(re.search(pattern, sentence, re.I) for pattern in patterns):
                    return Evidence(country, "high", sentence)
    return Evidence()


def country_in_person_note(person: Person, person_text: str) -> Evidence:
    context = " ".join(
        [
            person.role,
            person.affiliation,
            section(person_text, "Summary")[:800],
            section(person_text, "Related Entities"),
        ]
    )

    first_summary_sentence = ""
    summary = section(person_text, "Summary")
    if summary:
        first_summary_sentence = clean_snippet(re.split(r"(?<=[.!?])\s+", summary)[0], width=260)
        for country, aliases in COUNTRY_ALIASES.items():
            for alias in aliases:
                alias_re = re.escape(alias)
                if re.search(rf"\b{alias_re}\b(?:'s)?\s+{TITLE_WORDS}\b", first_summary_sentence, re.I):
                    return Evidence(country, "high", first_summary_sentence, f"{person.person_id}.md")
                if re.search(rf"\b{TITLE_WORDS}\b[^.]*\b(?:of|for|in)\s+\b{alias_re}\b", first_summary_sentence, re.I):
                    return Evidence(country, "high", first_summary_sentence, f"{person.person_id}.md")

    evidence = country_in_direct_text(context, person.display_name)
    if evidence.country:
        evidence.source = f"{person.person_id}.md"
    return evidence


def country_in_articles(
    person: Person,
    person_text: str,
    paths_by_stem: dict[str, Path],
    max_articles: int,
) -> Evidence:
    for target, label in coverage_links(person_text)[:max_articles]:
        path = paths_by_stem.get(target)
        if not path:
            continue
        summary = section(read_text(path), "Summary")
        evidence = country_in_direct_text(summary, person.display_name)
        if evidence.country:
            evidence.source = target
            if not evidence.evidence:
                evidence.evidence = clean_snippet(label)
            return evidence
    return Evidence()


def vault_inference(person: Person, paths_by_stem: dict[str, Path], max_articles: int) -> Evidence:
    path = PEOPLE_DIR / f"{person.person_id}.md"
    if not path.exists():
        return Evidence(evidence="person note missing")
    person_text = read_text(path)
    evidence = country_in_person_note(person, person_text)
    if evidence.country:
        return evidence
    return country_in_articles(person, person_text, paths_by_stem, max_articles)


def fetch_json(url: str, timeout: int, user_agent: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wikipedia_search(name: str, role: str, timeout: int, user_agent: str) -> list[dict]:
    query = name if not role else f"{name} {role}"
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": query,
            "srlimit": 5,
            "utf8": 1,
        }
    )
    url = f"https://en.wikipedia.org/w/api.php?{params}"
    data = fetch_json(url, timeout, user_agent)
    return data.get("query", {}).get("search", [])


def wikipedia_summary(title: str, timeout: int, user_agent: str) -> dict:
    encoded = urllib.parse.quote(title.replace(" ", "_"))
    url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
    return fetch_json(url, timeout, user_agent)


def internet_country_from_text(text: str, name: str) -> Evidence:
    lead = clean_snippet(text, width=900)
    for country, aliases in COUNTRY_ALIASES.items():
        for alias in aliases:
            alias_re = re.escape(alias)
            patterns = [
                rf"\b{name.split()[-1]}\b[^.]*\bis\s+(?:an?|the)?\s*\b{alias_re}\b\s+(?:politician|leader|official|minister|general|admiral|diplomat|journalist|analyst|economist)",
                rf"\b{name}\b[^.]*\bis\s+(?:an?|the)?\s*\b{alias_re}\b\s+(?:politician|leader|official|minister|general|admiral|diplomat|journalist|analyst|economist)",
                rf"\b{alias_re}\b(?:'s)?\s+{TITLE_WORDS}",
                rf"\b{TITLE_WORDS}\b[^.]*\b(?:of|for|in)\s+\b{alias_re}\b",
            ]
            if any(re.search(pattern, lead, re.I) for pattern in patterns):
                return Evidence(country, "high", clean_snippet(text, width=260))
    return Evidence()


def internet_confirmation(person: Person, timeout: int, user_agent: str) -> Evidence:
    try:
        results = wikipedia_search(person.display_name, person.role, timeout, user_agent)
    except Exception as exc:  # network/API failures become review evidence, not fatal.
        return Evidence(confidence="error", evidence=f"internet lookup failed: {exc}")

    for item in results:
        title = item.get("title", "")
        if not title:
            continue
        title_l = title.lower()
        name_l = person.display_name.lower()
        surname_l = person.display_name.split()[-1].lower()
        if surname_l not in title_l and title_l not in name_l and name_l not in title_l:
            continue
        try:
            summary = wikipedia_summary(title, timeout, user_agent)
        except Exception:
            continue
        text = " ".join([summary.get("title", ""), summary.get("description", ""), summary.get("extract", "")])
        evidence = internet_country_from_text(text, person.display_name)
        if evidence.country:
            evidence.source = f"Wikipedia: {summary.get('title', title)}"
            evidence.source_url = summary.get("content_urls", {}).get("desktop", {}).get("page", "")
            return evidence
        time.sleep(0.1)
    return Evidence(evidence="no internet confirmation found")


def choose_action(vault: Evidence, web: Evidence | None, internet_used: bool) -> tuple[str, str, str, str]:
    if not internet_used:
        if vault.country and vault.confidence == "high":
            return vault.country, "high", "update", "vault-only high-confidence evidence"
        return "", "none", "skip", "no high-confidence vault evidence"

    if vault.country and web and web.country == vault.country:
        return vault.country, "high", "update", "vault evidence confirmed by internet"
    if not vault.country and web and web.country:
        return web.country, "medium", "review", "internet-only confirmation; needs human approval"
    if vault.country and (not web or not web.country):
        return vault.country, "medium", "review", "vault evidence lacks internet confirmation"
    if vault.country and web and web.country and vault.country != web.country:
        return vault.country, "conflict", "review", f"vault suggests {vault.country}; internet suggests {web.country}"
    return "", "none", "skip", "no vault or internet confirmation"


def infer(args: argparse.Namespace) -> list[dict[str, str]]:
    people = load_people(include_known=args.include_known)
    if args.limit:
        people = people[: args.limit]
    paths_by_stem = article_paths()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for person in people:
        vault = vault_inference(person, paths_by_stem, args.max_articles)
        web = internet_confirmation(person, args.timeout, args.user_agent) if args.internet_confirm else None
        suggested, confidence, action, rationale = choose_action(vault, web, args.internet_confirm)
        rows.append(
            {
                "personId": person.person_id,
                "displayName": person.display_name,
                "mentionCount": str(person.mention_count),
                "currentCountry": person.country,
                "suggestedCountry": suggested,
                "confidence": confidence,
                "action": action,
                "rationale": rationale,
                "vaultCountry": vault.country,
                "vaultEvidence": vault.evidence,
                "vaultEvidenceFile": vault.source,
                "internetCountry": web.country if web else "",
                "internetEvidence": web.evidence if web else "",
                "internetSource": web.source if web else "",
                "internetUrl": web.source_url if web else "",
                "internetFetchedAt": fetched_at if args.internet_confirm else "",
            }
        )
    return sorted(rows, key=lambda r: ({"update": 0, "review": 1, "skip": 2}.get(r["action"], 9), -int(r["mentionCount"]), r["displayName"].lower()))


def write_csv(rows: list[dict[str, str]], out_path: str | None) -> None:
    fieldnames = [
        "personId",
        "displayName",
        "mentionCount",
        "currentCountry",
        "suggestedCountry",
        "confidence",
        "action",
        "rationale",
        "vaultCountry",
        "vaultEvidence",
        "vaultEvidenceFile",
        "internetCountry",
        "internetEvidence",
        "internetSource",
        "internetUrl",
        "internetFetchedAt",
    ]
    output = open(out_path, "w", encoding="utf-8", newline="") if out_path else sys.stdout
    try:
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if out_path:
            output.close()


def md_escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_markdown(rows: list[dict[str, str]], out_path: str | None) -> None:
    cols = [
        "personId",
        "displayName",
        "mentionCount",
        "suggestedCountry",
        "confidence",
        "action",
        "rationale",
        "vaultEvidenceFile",
        "internetSource",
        "internetUrl",
    ]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for row in rows:
        lines.append("| " + " | ".join(md_escape(row.get(col, "")) for col in cols) + " |")
    text = "\n".join(lines) + "\n"
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def print_summary(rows: list[dict[str, str]]) -> None:
    actions = Counter(row["action"] for row in rows)
    countries = Counter(row["suggestedCountry"] for row in rows if row["action"] == "update")
    print(
        f"Reviewed {len(rows)} people | "
        f"update {actions.get('update', 0)} | "
        f"review {actions.get('review', 0)} | "
        f"skip {actions.get('skip', 0)}",
        file=sys.stderr,
    )
    if countries:
        print("Update countries: " + ", ".join(f"{k}: {v}" for k, v in countries.most_common()), file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--include-known", action="store_true", help="include people whose country is already set")
    parser.add_argument("--limit", type=int, default=0, help="review only the first N candidates")
    parser.add_argument("--max-articles", type=int, default=80, help="max coverage articles to inspect per person")
    parser.add_argument("--internet-confirm", action="store_true", help="confirm via Wikipedia public API")
    parser.add_argument("--timeout", type=int, default=10, help="internet request timeout in seconds")
    parser.add_argument("--user-agent", default=os.environ.get("WIKI_COUNTRY_USER_AGENT", DEFAULT_USER_AGENT))
    parser.add_argument("--format", choices=["csv", "markdown"], default="csv")
    parser.add_argument("--output", help="write review table to a file instead of stdout")
    parser.add_argument("--summary", action="store_true", help="print counts to stderr")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = infer(args)
    if args.summary:
        print_summary(rows)
    if args.format == "markdown":
        write_markdown(rows, args.output)
    else:
        write_csv(rows, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
