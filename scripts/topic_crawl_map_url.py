#!/usr/bin/env python3
"""Map one canonical publisher URL to a NewsAPI.ai article URI safely."""
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

API_URL = "https://eventregistry.org/api/v1/articleMapper"


def mapped_uri(result: object, url: str) -> str | None:
    """Return a URI only from a usable mapper value; null requires fallback."""
    if not isinstance(result, dict):
        return None
    value = result.get(url)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("articleUri", "uri"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
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
    request = urllib.request.Request(
        f"{API_URL}?{urllib.parse.urlencode({'articleUrl': args.url, 'apiKey': key})}", method="GET")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            status, content_type, raw = response.status, response.headers.get_content_type(), response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"ERROR: mapper request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if status != 200 or content_type != "application/json":
        print(f"ERROR: unexpected mapper response status={status} contentType={content_type}", file=sys.stderr)
        return 2
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: mapper response is not valid JSON: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    uri = mapped_uri(result, args.url)
    print(json.dumps({"status": status, "mappingStatus": "mapped" if uri else "unusable",
                      "articleUri": uri, "fallbackRequired": uri is None, "output": str(args.output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
