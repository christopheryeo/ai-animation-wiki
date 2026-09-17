#!/usr/bin/env python3
"""Validate source-backed SET B grounded-discovery evidence before URL mapping."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlparse


GEOGRAPHY_STATUSES = {"qualifying", "out-of-scope", "not-established"}


def nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_candidate(candidate: Any, index: int) -> list[str]:
    prefix = f"returnedUrls[{index}]"
    if not isinstance(candidate, dict):
        return [f"{prefix}: must be an object"]
    errors: list[str] = []
    url = candidate.get("url")
    parsed = urlparse(url) if nonempty(url) else None
    if not parsed or parsed.scheme not in {"http", "https"} or not parsed.netloc:
        errors.append(f"{prefix}.url: must be an absolute http(s) URL")
    domain = candidate.get("sourceDomain")
    if not nonempty(domain):
        errors.append(f"{prefix}.sourceDomain: is required")
    elif parsed and parsed.hostname and parsed.hostname.lower() != str(domain).lower().lstrip(".") and not parsed.hostname.lower().endswith("." + str(domain).lower().lstrip(".")):
        errors.append(f"{prefix}.sourceDomain: must match the URL host")
    if candidate.get("isDirectArticle") is not True:
        errors.append(f"{prefix}.isDirectArticle: must be true")
    if not nonempty(candidate.get("title")):
        errors.append(f"{prefix}.title: requires attributable title evidence")

    publication = candidate.get("publicationEvidence")
    if not isinstance(publication, dict):
        errors.append(f"{prefix}.publicationEvidence: is required")
    else:
        value = publication.get("date")
        if not nonempty(value):
            errors.append(f"{prefix}.publicationEvidence.date: is required")
        else:
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{prefix}.publicationEvidence.date: must be YYYY-MM-DD")
        if not nonempty(publication.get("evidence")):
            errors.append(f"{prefix}.publicationEvidence.evidence: is required")

    geography = candidate.get("geographyEvidence")
    if not isinstance(geography, dict):
        errors.append(f"{prefix}.geographyEvidence: is required")
    else:
        status = geography.get("status")
        if status not in GEOGRAPHY_STATUSES:
            errors.append(f"{prefix}.geographyEvidence.status: must be one of {sorted(GEOGRAPHY_STATUSES)}")
        if not nonempty(geography.get("evidence")):
            errors.append(f"{prefix}.geographyEvidence.evidence: is required")
        if status == "qualifying" and not nonempty(geography.get("relationship")):
            errors.append(f"{prefix}.geographyEvidence.relationship: is required when qualifying")
    return errors


def validate_manifest(manifest: Any) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest: must be an object"]
    errors: list[str] = []
    if not nonempty(manifest.get("environment")):
        errors.append("environment: is required")
    if not nonempty(manifest.get("request")):
        errors.append("request: is required")
    candidates = manifest.get("returnedUrls")
    if not isinstance(candidates, list):
        return errors + ["returnedUrls: must be a list"]
    for index, candidate in enumerate(candidates):
        errors.extend(validate_candidate(candidate, index))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: cannot read manifest: {exc}", file=sys.stderr)
        return 2
    errors = validate_manifest(manifest)
    print(json.dumps({"manifest": str(args.manifest), "valid": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
