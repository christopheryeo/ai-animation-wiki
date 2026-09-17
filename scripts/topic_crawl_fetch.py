#!/usr/bin/env python3
"""Fetch one bounded NewsAPI.ai Set A page without exposing its credential."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

from local_env import load_local_env
import os


API_URL = "https://eventregistry.org/api/v1/article/getArticles"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", required=True)
    parser.add_argument("--date-start", required=True)
    parser.add_argument("--date-end", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.page < 1:
        raise SystemExit("--page must be positive")
    load_local_env()
    key = os.environ.get("NEWSAPI_AI_API_KEY")
    if not key:
        raise SystemExit("NEWSAPI_AI_API_KEY is unavailable")
    payload = {
        "action": "getArticles", "keyword": args.keyword, "lang": ["eng"],
        "dateStart": args.date_start, "dateEnd": args.date_end, "articlesSortBy": "date",
        "articleBodyLen": -1, "dataType": ["news"], "isDuplicateFilter": "keepAll",
        "resultType": "articles", "articlesCount": 100, "articlesPage": args.page, "apiKey": key,
    }
    request = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status = response.status
            content_type = response.headers.get_content_type()
            raw = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"ERROR: provider request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if status != 200 or content_type != "application/json":
        print(f"ERROR: unexpected provider response status={status} contentType={content_type}", file=sys.stderr)
        return 2
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: provider response is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(result.get("articles"), dict):
        print("ERROR: provider response lacks articles object", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    articles = result["articles"]
    print(json.dumps({"status": status, "page": articles.get("page"), "pages": articles.get("pages"),
                      "count": articles.get("count"), "totalResults": articles.get("totalResults"),
                      "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
