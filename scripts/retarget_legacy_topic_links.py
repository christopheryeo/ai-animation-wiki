#!/usr/bin/env python3
"""Retarget topic links using an explicitly supplied JSON mapping.

The starter ships with no legacy mappings and therefore performs no writes
unless a reviewer supplies a mapping file and omits --dry-run.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    if not isinstance(mapping, dict):
        raise SystemExit("mapping must be a JSON object")
    print(json.dumps({"status": "ready", "mappingCount": len(mapping), "dryRun": args.dry_run}))
    if mapping and not args.dry_run:
        raise SystemExit("apply mode requires a project-specific reviewed implementation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
