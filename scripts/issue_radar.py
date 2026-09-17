#!/usr/bin/env python3
"""Early-warning issue radar over canonical MySQL product tables.

The radar reads articles, article_tags, and article_coverage using a read-only,
consistent-snapshot transaction. UAT is the safe default; production requires
an explicit ``--source production`` selection. It never writes to MySQL or the
Markdown vault.

Authentication is delegated to the MySQL client. Prefer ``--defaults-file`` or
``--login-path`` with a SELECT-only account. Environment variables are also
supported; see ``--help``. Source-specific
``ISSUE_RADAR_UAT_MYSQL_*``/``ISSUE_RADAR_PRODUCTION_MYSQL_*`` values override
generic ``ISSUE_RADAR_MYSQL_*`` and ``DB_*`` values. They may be placed in the
Git-ignored ``.env.local`` (see ``scripts/local_env.py``). A real shell export
always takes precedence over ``.env.local``.
"""

import argparse
import collections
import csv
import datetime
import getpass
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from local_env import load_local_env  # noqa: E402
from tag_registry import load_registry as load_tag_registry  # noqa: E402
from tag_registry import normalize_tag, radar_status_at, status_at  # noqa: E402


INSTITUTIONAL = {
    "Animation Studio", "Character and IP", "Merchandising and Licensing",
    "Distribution and Platforms", "Technology and Production", "Industry Policy",
    "Studio Business",
}
STOP = {
    "animation", "character", "studio", "industry", "business", "content",
    "film", "series", "media", "entertainment", "united states", "southeast asia",
}
GENERIC_FRACTION = 0.03
MIN_ARTICLES, MIN_WEEKS = 8, 3
WINDOW = 28

WEIGHTS = dict(accel=0.25, breadth=0.20, inst=0.25, recur=0.15, unfac=0.10, opin=0.05)
TIERS = [("HOT", 0.60, 8), ("WARM", 0.40, 4), ("WATCH", 0.25, 2)]
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")

SOURCE_DEFAULTS = {
    "uat": ("AI_Animation_UAT", "UAT_"),
    "production": ("AI_Animation", ""),
}


class RadarError(RuntimeError):
    """A user-facing radar configuration or data-source error."""


def monday(day):
    return day - datetime.timedelta(days=day.weekday())


def safe_identifier(value, label, allow_empty=False):
    if allow_empty and value == "":
        return value
    if not value or not IDENTIFIER.fullmatch(value):
        raise RadarError(f"invalid {label}: {value!r}")
    return value


def product_query(database, prefix):
    """Return the single read-only query used for both UAT and production."""
    database = safe_identifier(database, "database")
    prefix = safe_identifier(prefix, "table prefix", allow_empty=True)
    articles = f"`{database}`.`{prefix}articles`"
    tags = f"`{database}`.`{prefix}article_tags`"
    coverage = f"`{database}`.`{prefix}article_coverage`"
    return f"""
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT;
SELECT JSON_OBJECT(
  'article_id', a.article_id,
  'title', COALESCE(a.article_title, ''),
  'published_date', DATE_FORMAT(a.published_date, '%Y-%m-%dT%H:%i:%s'),
  'category', COALESCE(a.category, ''),
  'tone', COALESCE(a.tone, ''),
  'event_type', COALESCE(a.event_type, ''),
  'tags', COALESCE(t.tags, JSON_ARRAY()),
  'outlets', COALESCE(c.outlets, JSON_ARRAY()),
  'countries', COALESCE(c.countries, JSON_ARRAY())
)
FROM {articles} a
LEFT JOIN (
  SELECT article_id, JSON_ARRAYAGG(tag) AS tags
  FROM {tags}
  GROUP BY article_id
) t ON t.article_id = a.article_id
LEFT JOIN (
  SELECT article_id,
         JSON_ARRAYAGG(display_name) AS outlets,
         JSON_ARRAYAGG(country) AS countries
  FROM {coverage}
  GROUP BY article_id
) c ON c.article_id = a.article_id
WHERE a.published_date IS NOT NULL
  AND (a.article_status = 'A' OR a.article_status IS NULL)
ORDER BY a.article_id;
COMMIT;
"""


def product_tags_query(database, prefix):
    """Return a read-only inventory query for every distinct product tag."""
    database = safe_identifier(database, "database")
    prefix = safe_identifier(prefix, "table prefix", allow_empty=True)
    tags = f"`{database}`.`{prefix}article_tags`"
    return f"""
SET SESSION TRANSACTION READ ONLY;
START TRANSACTION WITH CONSISTENT SNAPSHOT;
SELECT JSON_OBJECT('source_tag', MIN(tag), 'article_count', COUNT(DISTINCT article_id))
FROM {tags}
GROUP BY BINARY tag
ORDER BY BINARY MIN(tag);
COMMIT;
"""


def source_setting(args, suffix, fallback=None):
    """Return a source-specific connection value before a generic fallback."""
    source = getattr(args, "source", None)
    if source:
        value = os.environ.get(f"ISSUE_RADAR_{source.upper()}_MYSQL_{suffix}")
        if value:
            return value
    return fallback


def mysql_command(args):
    command = [source_setting(args, "PROGRAM", args.mysql_program)]
    defaults_file = source_setting(args, "DEFAULTS_FILE", args.defaults_file)
    login_path = source_setting(args, "LOGIN_PATH", args.login_path)
    host = source_setting(args, "HOST", args.mysql_host)
    port = source_setting(args, "PORT", args.mysql_port)
    user = source_setting(args, "USER", args.mysql_user)
    ssl_mode = source_setting(args, "SSL_MODE", args.ssl_mode)
    if defaults_file:
        command.append(f"--defaults-extra-file={defaults_file}")
    if login_path:
        command.append(f"--login-path={login_path}")
    command.extend(["--batch", "--raw", "--skip-column-names", "--default-character-set=utf8mb4"])
    if host:
        command.extend(["--host", str(host)])
    if port:
        command.extend(["--port", str(port)])
    if user:
        command.extend(["--user", str(user)])
    if ssl_mode:
        command.append(f"--ssl-mode={ssl_mode}")
    return command


def parse_product_rows(output):
    articles = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            published = datetime.datetime.fromisoformat(row["published_date"]).date()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RadarError(f"invalid MySQL row {line_number}: {exc}") from exc

        def clean_set(values):
            return {str(value).strip() for value in (values or []) if value is not None and str(value).strip()}

        articles.append({
            "id": int(row["article_id"]),
            "title": row.get("title") or "",
            "date": published,
            "cat": row.get("category") or "",
            "tags": {value.lower() for value in clean_set(row.get("tags"))},
            "outlets": clean_set(row.get("outlets")),
            "countries": clean_set(row.get("countries")),
            "unfac": row.get("event_type") == "Unfacilitated",
            "opin": row.get("tone") == "Opinionated",
        })
    if not articles:
        raise RadarError("the product-table query returned no articles")
    return articles


def resolve_source(args):
    database, prefix = SOURCE_DEFAULTS[args.source]
    return args.database or database, args.table_prefix if args.table_prefix is not None else prefix


def run_mysql(args, query):
    environment = os.environ.copy()
    password = (
        getpass.getpass("MySQL password: ")
        if args.prompt_password
        else (
            source_setting(args, "PASSWORD")
            or os.environ.get("ISSUE_RADAR_MYSQL_PASSWORD")
            or os.environ.get("DB_PASSWORD")
        )
    )
    if password:
        environment["MYSQL_PWD"] = password
    try:
        result = subprocess.run(
            mysql_command(args), input=query, text=True, capture_output=True,
            env=environment, check=False,
        )
    except FileNotFoundError as exc:
        raise RadarError(f"MySQL client not found: {args.mysql_program}") from exc
    if result.returncode:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown MySQL error"
        raise RadarError(f"product-table query failed: {detail}")
    return result.stdout


def load_articles(args):
    database, prefix = resolve_source(args)
    output = run_mysql(args, product_query(database, prefix))
    return parse_product_rows(output), database, prefix


def apply_tag_registry(
    articles, asof, records=None, radar_statuses=None, use_current_radar_status=False,
):
    """Reject unknown tags and retain lifecycle-active tags in selected radar states."""

    records = load_tag_registry() if records is None else records
    radar_statuses = {"enabled"} if radar_statuses is None else set(radar_statuses)
    lookup = {normalize_tag(record.display_name): record for record in records}
    output = []
    for article in articles:
        active_tags = set()
        for raw in article["tags"]:
            key = normalize_tag(raw)
            record = lookup.get(key)
            if record is None:
                raise RadarError(
                    f"database tag has no Tag entity: article={article['id']} tag={raw!r}"
                )
            if (
                status_at(record, asof) == "active"
                and (
                    record.radar_status if use_current_radar_status else radar_status_at(record, asof)
                ) in radar_statuses
            ):
                active_tags.add(normalize_tag(record.display_name))
        output.append({**article, "tags": active_tags})
    return output


def export_tags(args, destination):
    database, prefix = resolve_source(args)
    output = run_mysql(args, product_tags_query(database, prefix))
    rows = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            source_tag = row["source_tag"]
            rows.append((source_tag, source_tag.strip().lower(), int(row["article_count"])))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise RadarError(f"invalid tag-inventory row {line_number}: {exc}") from exc
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["source_tag", "radar_tag", "article_count"])
        writer.writerows(rows)
    print(f"Exported {len(rows)} distinct tags from {database}.{prefix}article_tags to {path}")


def candidates(articles, asof):
    """Return tag candidates using only records visible at the evaluation date."""
    eligible = [article for article in articles if article["date"] <= asof]
    frequency = collections.Counter(tag for article in eligible for tag in article["tags"])
    cap = len(eligible) * GENERIC_FRACTION
    output = {}
    for tag, count in frequency.items():
        if count < MIN_ARTICLES or count > cap or tag in STOP or len(tag) < 3:
            continue
        selected = [article for article in eligible if tag in article["tags"]]
        if len({monday(article["date"]) for article in selected}) >= MIN_WEEKS:
            output[tag] = selected
    return output


def waves(dates, asof):
    """Count active-week runs separated by at least two silent weeks."""
    weeks = sorted({monday(day) for day in dates if day <= asof})
    if not weeks:
        return 0
    count, previous = 1, weeks[0]
    for week in weeks[1:]:
        if (week - previous).days > 14:
            count += 1
        previous = week
    return count


def classify_tier(score, recent_volume):
    """Return the configured tier for a score/volume pair."""
    return next(
        (name for name, minimum, volume in TIERS if score >= minimum and recent_volume >= volume),
        None,
    )


def score_issue(selected, asof):
    history = [article for article in selected if article["date"] <= asof]
    if not history:
        return None
    recent_start = asof - datetime.timedelta(days=WINDOW)
    prior_start = asof - datetime.timedelta(days=2 * WINDOW)
    recent = [article for article in history if article["date"] > recent_start]
    prior = [article for article in history if prior_start < article["date"] <= recent_start]
    before = [article for article in history if article["date"] <= recent_start]
    if not recent:
        return None

    recent_volume, prior_volume = len(recent), len(prior)
    acceleration = min(1.0, (recent_volume / max(prior_volume, 1)) / 4.0) if recent_volume >= 3 else 0.0

    seen_outlets = set().union(*(article["outlets"] for article in before)) if before else set()
    seen_countries = set().union(*(article["countries"] for article in before)) if before else set()
    new_outlets = set().union(*(article["outlets"] for article in recent)) - seen_outlets
    new_countries = set().union(*(article["countries"] for article in recent)) - seen_countries
    breadth = min(1.0, len(new_outlets) / 10.0 + len(new_countries) / 4.0) if before else 0.3

    institutional_recent = sum(article["cat"] in INSTITUTIONAL for article in recent) / recent_volume
    institutional_before = (
        sum(article["cat"] in INSTITUTIONAL for article in before) / len(before)
        if before else 0.0
    )
    institutional = min(
        1.0,
        institutional_recent * 0.6
        + max(0.0, institutional_recent - institutional_before) * 0.8,
    )

    wave_count = waves([article["date"] for article in history], asof)
    recurrence = min(1.0, (wave_count - 1) / 3.0)
    unfacilitated = sum(article["unfac"] for article in recent) / recent_volume
    opinionated = sum(article["opin"] for article in recent) / recent_volume

    parts = {
        "accel": acceleration, "breadth": breadth, "inst": institutional,
        "recur": recurrence, "unfac": unfacilitated, "opin": opinionated,
    }
    score = sum(WEIGHTS[name] * value for name, value in parts.items())
    tier = classify_tier(score, recent_volume)
    reasons = []
    if acceleration > 0.3:
        reasons.append(f"volume {prior_volume}->{recent_volume} over two {WINDOW}d windows")
    if new_outlets:
        reasons.append(f"{len(new_outlets)} never-seen outlets")
    if new_countries:
        reasons.append(f"new countries: {', '.join(sorted(new_countries)[:4])}")
    if institutional_recent > 0.3:
        reasons.append(f"{institutional_recent:.0%} of recent coverage in institutional categories")
    if institutional_recent - institutional_before > 0.2 and before:
        reasons.append(f"institutional share rose {institutional_before:.0%}->{institutional_recent:.0%}")
    if wave_count >= 2:
        reasons.append(f"{wave_count} distinct coverage waves (recurring, not dying)")
    if unfacilitated > 0.7:
        reasons.append(f"{unfacilitated:.0%} unfacilitated (story running on its own)")
    if opinionated > 0.15:
        reasons.append(f"{opinionated:.0%} opinionated pieces")
    return {"score": score, "tier": tier, "vol": recent_volume, "parts": parts, "why": reasons}


def structured_run(
    articles, database, prefix, asof, candidate_count, ranked, min_tier, selections=None,
    registry_metadata=None, radar_mode="operational",
):
    """Return deterministic structured output for audit and run comparison."""
    selections = selections or {}
    recent_start = asof - datetime.timedelta(days=WINDOW)
    return {
        "schemaVersion": "issue-radar-run.v1",
        "asOf": asof.isoformat(),
        "source": {
            "database": database,
            "tablePrefix": prefix,
            "articleTable": f"{prefix}articles",
            "tagTable": f"{prefix}article_tags",
            "coverageTable": f"{prefix}article_coverage",
            "readOnly": True,
        },
        "articleCount": len(articles),
        "candidateCount": candidate_count,
        "flagCount": len(ranked),
        "minimumTier": min_tier,
        "radarMode": radar_mode,
        "tagRegistry": registry_metadata or {},
        "configuration": {
            "windowDays": WINDOW,
            "minimumArticles": MIN_ARTICLES,
            "minimumWeeks": MIN_WEEKS,
            "genericFraction": GENERIC_FRACTION,
            "weights": dict(sorted(WEIGHTS.items())),
            "tiers": [
                {"name": name, "minimumScore": minimum, "minimumRecentVolume": volume}
                for name, minimum, volume in TIERS
            ],
        },
        "flags": [
            {
                "tag": tag,
                "tier": result["tier"],
                "score": result["score"],
                "recentVolume": result["vol"],
                "signals": dict(sorted(result["parts"].items())),
                "reasons": result["why"],
                "articleIds": sorted(
                    article["id"]
                    for article in selections.get(tag, [])
                    if article["date"] <= asof
                ),
                "recentArticleIds": sorted(
                    article["id"]
                    for article in selections.get(tag, [])
                    if recent_start < article["date"] <= asof
                ),
            }
            for tag, result in ranked
        ],
    }


def write_structured_run(path, payload):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_text_output(path, content):
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def ranked_report(asof, ranked, article_count, top, source_label):
    lines = [
        (
            f"# Issue radar as of {asof} "
            f"({len(ranked)} flagged from {article_count} articles, showing top {top}; "
            f"source {source_label})"
        ),
        "",
    ]
    for tag, result in ranked[:top]:
        lines.append(f"[{result['tier']:<5}] {result['score']:.2f}  {tag}  (vol {result['vol']}/28d)")
        lines.extend(f"         - {reason}" for reason in result["why"])
    if not ranked:
        lines.append("nothing flagged")
    return "\n".join(lines) + "\n"


def series(selected, asof):
    weekly = collections.defaultdict(lambda: [0, set(), set()])
    for article in selected:
        if article["date"] > asof:
            continue
        bucket = weekly[monday(article["date"]).isoformat()]
        bucket[0] += 1
        bucket[1] |= article["outlets"]
        bucket[2] |= article["countries"]
    return {week: (count, len(outlets), len(countries)) for week, (count, outlets, countries) in sorted(weekly.items())}


def build_parser():
    parser = argparse.ArgumentParser(
        description="Read-only issue radar over canonical MySQL product tables",
        epilog=(
            "Connection values may also come from source-specific "
            "ISSUE_RADAR_UAT_MYSQL_* or ISSUE_RADAR_PRODUCTION_MYSQL_* variables, then "
            "generic ISSUE_RADAR_MYSQL_HOST, _PORT, _USER, _PASSWORD, _DEFAULTS_FILE, "
            "_LOGIN_PATH, _PROGRAM, and _SSL_MODE. "
            "Prefer a MySQL option file or login path over a password environment variable."
        ),
    )
    parser.add_argument("--source", choices=sorted(SOURCE_DEFAULTS), default="uat",
                        help="uat (default) or production; controls database/table defaults")
    parser.add_argument("--database", help="override the selected MySQL database")
    parser.add_argument("--table-prefix", help="override the table prefix, e.g. UAT_ or empty")
    parser.add_argument("--mysql-program", default=os.environ.get("ISSUE_RADAR_MYSQL_PROGRAM", "mysql"))
    parser.add_argument("--defaults-file", default=os.environ.get("ISSUE_RADAR_MYSQL_DEFAULTS_FILE"),
                        help="MySQL option file; preferred for automation")
    parser.add_argument("--login-path", default=os.environ.get("ISSUE_RADAR_MYSQL_LOGIN_PATH"),
                        help="mysql_config_editor login path")
    parser.add_argument(
        "--mysql-host",
        default=os.environ.get("ISSUE_RADAR_MYSQL_HOST") or os.environ.get("DB_HOST"),
    )
    parser.add_argument(
        "--mysql-port",
        type=int,
        default=int(
            os.environ.get("ISSUE_RADAR_MYSQL_PORT")
            or os.environ.get("DB_PORT")
            or "0"
        ) or None,
    )
    parser.add_argument(
        "--mysql-user",
        default=os.environ.get("ISSUE_RADAR_MYSQL_USER") or os.environ.get("DB_USER"),
    )
    parser.add_argument("--prompt-password", action="store_true",
                        help="securely prompt for the MySQL password without putting it in arguments")
    parser.add_argument("--ssl-mode", choices=["DISABLED", "PREFERRED", "REQUIRED", "VERIFY_CA", "VERIFY_IDENTITY"],
                        default=os.environ.get("ISSUE_RADAR_MYSQL_SSL_MODE"))
    parser.add_argument("--asof", help="historical evaluation date, YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--issue", help="show one exact tag or title-matched weekly series")
    parser.add_argument("--min-tier", default="WATCH", choices=[tier[0] for tier in TIERS])
    parser.add_argument("--export-tags", metavar="CSV_PATH",
                        help="export every distinct source tag and article count, then exit")
    parser.add_argument("--json-output", metavar="JSON_PATH",
                        help="write the complete deterministic structured radar result")
    parser.add_argument("--text-output", metavar="TEXT_PATH",
                        help="write the complete readable radar report as text")
    parser.add_argument("--shadow-json-output", metavar="JSON_PATH",
                        help="write comparison-only results for shadow tags")
    parser.add_argument("--shadow-text-output", metavar="TEXT_PATH",
                        help="write the readable comparison report for shadow tags")
    parser.add_argument("--comparison-json-output", metavar="JSON_PATH",
                        help="write enabled-plus-shadow pre-optimization comparison results")
    parser.add_argument("--comparison-text-output", metavar="TEXT_PATH",
                        help="write enabled-plus-shadow readable comparison results")
    parser.add_argument("--use-current-radar-status", action="store_true",
                        help="backtest the current eligibility state at a historical evaluation date")
    return parser


def tag_registry_metadata(records):
    rows = [
        {
            "tagId": record.tag_id,
            "lifecycleStatus": record.status,
            "radarStatus": record.radar_status,
            "radarStatusEffectiveAt": record.radar_status_effective_at,
        }
        for record in sorted(records, key=lambda item: item.tag_id)
    ]
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "tagCount": len(records),
        "radarStatusCounts": dict(sorted(collections.Counter(r.radar_status for r in records).items())),
    }


def rank_articles(articles, asof, min_tier):
    selected_candidates = candidates(articles, asof)
    ranked = []
    for tag, selected in selected_candidates.items():
        result = score_issue(selected, asof)
        if result and result["tier"]:
            ranked.append((tag, result))
    order = {tier[0]: index for index, tier in enumerate(TIERS)}
    keep = order[min_tier]
    ranked = [item for item in ranked if order[item[1]["tier"]] <= keep]
    ranked.sort(key=lambda item: (-item[1]["score"], item[0]))
    return selected_candidates, ranked


def run(args):
    if args.export_tags:
        export_tags(args, args.export_tags)
        return
    raw_articles, database, prefix = load_articles(args)
    asof = datetime.date.fromisoformat(args.asof) if args.asof else max(article["date"] for article in raw_articles)
    records = load_tag_registry()
    registry_meta = tag_registry_metadata(records)
    articles = apply_tag_registry(
        raw_articles, asof, records, {"enabled"}, args.use_current_radar_status,
    )
    source_label = f"{database}.{prefix}articles"

    if args.issue:
        needle = args.issue.strip().lower()
        selected = [
            article for article in articles
            if article["date"] <= asof and (needle in article["tags"] or needle in article["title"].lower())
        ]
        print(
            f"# series for '{args.issue}' as of {asof} "
            f"({len(selected)} matched of {len(articles)} articles; source {source_label})"
        )
        for week, (count, outlet_count, country_count) in series(selected, asof).items():
            print(f"{week}  vol={count:<4} outlets={outlet_count:<4} countries={country_count}")
        result = score_issue(selected, asof)
        if result:
            print(f"\nscore={result['score']:.2f} tier={result['tier']}  " + "; ".join(result["why"]))
        return

    selected_candidates, ranked = rank_articles(articles, asof, args.min_tier)
    if args.json_output:
        write_structured_run(
            args.json_output,
            structured_run(
                articles, database, prefix, asof, len(selected_candidates), ranked, args.min_tier,
                selected_candidates,
                registry_meta,
                "operational",
            ),
        )

    report = ranked_report(asof, ranked, len(articles), args.top, source_label)
    if args.text_output:
        write_text_output(args.text_output, report)
    print(report, end="")

    if args.shadow_json_output or args.shadow_text_output:
        shadow_articles = apply_tag_registry(
            raw_articles, asof, records, {"shadow"}, args.use_current_radar_status,
        )
        shadow_candidates, shadow_ranked = rank_articles(shadow_articles, asof, args.min_tier)
        if args.shadow_json_output:
            write_structured_run(
                args.shadow_json_output,
                structured_run(
                    shadow_articles, database, prefix, asof, len(shadow_candidates),
                    shadow_ranked, args.min_tier, shadow_candidates, registry_meta, "shadow",
                ),
            )
        if args.shadow_text_output:
            write_text_output(
                args.shadow_text_output,
                ranked_report(asof, shadow_ranked, len(shadow_articles), args.top, source_label),
            )
    if args.comparison_json_output or args.comparison_text_output:
        comparison_articles = apply_tag_registry(
            raw_articles, asof, records, {"enabled", "shadow"},
            args.use_current_radar_status,
        )
        comparison_candidates, comparison_ranked = rank_articles(
            comparison_articles, asof, args.min_tier
        )
        if args.comparison_json_output:
            write_structured_run(
                args.comparison_json_output,
                structured_run(
                    comparison_articles, database, prefix, asof,
                    len(comparison_candidates), comparison_ranked, args.min_tier,
                    comparison_candidates, registry_meta, "enabled-plus-shadow-comparison",
                ),
            )
        if args.comparison_text_output:
            write_text_output(
                args.comparison_text_output,
                ranked_report(
                    asof, comparison_ranked, len(comparison_articles), args.top, source_label,
                ),
            )


def main():
    load_local_env()
    try:
        run(build_parser().parse_args())
    except (RadarError, ValueError) as exc:
        print(f"issue_radar: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
