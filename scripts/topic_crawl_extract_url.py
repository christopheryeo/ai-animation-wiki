#!/usr/bin/env python3
"""Run the approved NewsAPI.ai extraction fallback for one publisher URL."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

from local_env import load_local_env


API_URL = "https://analytics.eventregistry.org/api/v1/extractArticleInfo"

# Meta keys that carry a publication instant, in preference order.
_DATE_META_KEYS = (
    "article:published_time", "og:published_time", "publishdate", "publish-date",
    "date", "dc.date", "dc.date.issued", "datepublished", "sailthru.date",
    "parsely-pub-date", "datemodified",
)


def recover_date(value: dict) -> str | None:
    """Best-effort in-source publication date (YYYY-MM-DD) for the SET B date gate.

    Order: provider top-level fields, then <meta> published-time tags, then JSON-LD
    ``datePublished``. Returns None when no attributable date is present — the caller
    must then HOLD the candidate, never guess a date.
    """
    for key in ("date", "datetime", "dateTime", "dateTimePub"):
        raw = value.get(key)
        if isinstance(raw, str) and raw[:4].isdigit():
            return raw[:10]
    meta = value.get("meta")
    if isinstance(meta, dict):
        lower = {str(k).lower(): v for k, v in meta.items()}
        for key in _DATE_META_KEYS:
            raw = lower.get(key)
            if isinstance(raw, str) and raw[:4].isdigit():
                return raw[:10]
    jsonld = value.get("jsonld")
    if isinstance(jsonld, list):
        for item in jsonld:
            try:
                obj = json.loads(item) if isinstance(item, str) else item
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(obj, dict):
                raw = obj.get("datePublished") or obj.get("dateCreated") or obj.get("uploadDate")
                if isinstance(raw, str) and raw[:4].isdigit():
                    return raw[:10]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_local_env()
    key = os.environ.get("NEWSAPI_AI_API_KEY")
    if not key:
        raise SystemExit("NEWSAPI_AI_API_KEY is unavailable")
    request = urllib.request.Request(f"{API_URL}?{urllib.parse.urlencode({'url': args.url, 'apiKey': key})}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, content_type, raw = response.status, response.headers.get_content_type(), response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"ERROR: extraction request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if status != 200 or content_type != "application/json":
        print(f"ERROR: unexpected extraction response status={status} contentType={content_type}", file=sys.stderr)
        return 2
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: extraction response is not valid JSON: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    recovered = recover_date(value) if isinstance(value, dict) else None
    print(json.dumps({
        "status": status,
        "topLevelKeys": sorted(value) if isinstance(value, dict) else [],
        "recoveredDate": recovered,
        "bodyLen": len(value.get("body") or "") if isinstance(value, dict) else 0,
        "dateGateReady": bool(recovered and isinstance(value, dict) and (value.get("body") or "").strip()),
        "output": str(args.output),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
