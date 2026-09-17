#!/usr/bin/env python3
"""Retrieve one NewsAPI.ai article by URI (plan Step 6) without exposing its credential."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request

from local_env import load_local_env
import os


API_URL = "https://eventregistry.org/api/v1/article/getArticle"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    load_local_env()
    key = os.environ.get("NEWSAPI_AI_API_KEY")
    if not key:
        raise SystemExit("NEWSAPI_AI_API_KEY is unavailable")
    payload = {
        "action": "getArticle", "articleUri": args.uri, "infoArticleBodyLen": -1,
        "resultType": "info", "apiKey": key,
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
    node = result.get(args.uri)
    info = node.get("info") if isinstance(node, dict) and isinstance(node.get("info"), dict) else (
        node if isinstance(node, dict) and node.get("body") else None
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    print(json.dumps({
        "status": status, "uri": args.uri,
        "title": (info or {}).get("title", "")[:100],
        "date": (info or {}).get("date"),
        "bodyLen": len((info or {}).get("body") or ""),
        "source": ((info or {}).get("source") or {}).get("title"),
        "usable": bool(info and (info.get("body") or "").strip() and info.get("date")),
        "output": str(args.output),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
