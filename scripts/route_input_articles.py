#!/usr/bin/env python3
"""Route loose crawler articles into Inputs/articles/YYYY-MM by publication date.

The script reads only loose Markdown files directly under Inputs/articles. It
does not read or modify raw source evidence. Default mode is a dry run; --write
moves each valid file without changing its contents. Existing destinations and
already-compiled article names are reported and never overwritten.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from enrich_radar_inputs import parse_frontmatter, split_note  # noqa: E402


def publication_month(value: Any) -> str:
    if value is None or not str(value).strip():
        raise ValueError("publishedDate is missing")
    parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return parsed.strftime("%Y-%m")


def route_loose_articles(
    input_root: Path,
    article_root: Path,
    write: bool = False,
    selected_names: set[str] | None = None,
) -> dict[str, Any]:
    files = sorted(
        path for path in input_root.glob("*.md")
        if path.is_file() and path.name != ".DS_Store"
    )
    missing_selected: list[str] = []
    if selected_names is not None:
        existing_names = {path.name for path in files}
        missing_selected = sorted(selected_names - existing_names)
        files = [path for path in files if path.name in selected_names]
    moved = []
    planned = []
    blocked = [
        {"source": str(input_root / name), "error": "manifest input is missing"}
        for name in missing_selected
    ]
    for path in files:
        try:
            lines, _ = split_note(path.read_text(encoding="utf-8"))
            metadata = parse_frontmatter(lines)
            month = publication_month(metadata.get("publishedDate"))
            target = input_root / month / path.name
            compiled = article_root / month / path.name
            if target.exists():
                raise ValueError(f"input destination already exists: {target}")
            if compiled.exists():
                raise ValueError(f"compiled article already exists: {compiled}")
            item = {
                "source": str(path),
                "destination": str(target),
                "month": month,
                "articleId": str(metadata.get("articleId") or ""),
            }
            planned.append(item)
            if write:
                target.parent.mkdir(parents=True, exist_ok=True)
                path.replace(target)
                moved.append(item)
        except Exception as exc:
            blocked.append({"source": str(path), "error": str(exc)})
    return {
        "mode": "write" if write else "dry-run",
        "looseFiles": len(files) + len(missing_selected),
        "planned": len(planned),
        "moved": len(moved),
        "blocked": len(blocked),
        "routes": planned,
        "errors": blocked,
    }


def build_frozen_manifest(input_root: Path) -> dict[str, Any]:
    """Capture identity and content hashes for the current loose Markdown batch."""

    articles = []
    for path in sorted(
        candidate for candidate in input_root.glob("*.md")
        if candidate.is_file() and candidate.name != ".DS_Store"
    ):
        raw = path.read_bytes()
        lines, _ = split_note(raw.decode("utf-8"))
        metadata = parse_frontmatter(lines)
        articles.append({
            "path": str(path.relative_to(ROOT)),
            "filename": path.name,
            "articleId": str(metadata.get("articleId") or ""),
            "title": str(metadata.get("articleTitle") or ""),
            "publishedDate": str(metadata.get("publishedDate") or ""),
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    return {
        "schemaVersion": "loose-article-manifest.v1",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "articleCount": len(articles),
        "articles": articles,
    }


def load_manifest_names(path: Path) -> set[str]:
    """Load a frozen direct-input manifest and return its unique filenames."""

    entries = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not entries:
        raise ValueError(f"manifest contains no paths: {path}")
    names = [Path(entry).name for entry in entries]
    if any(not name.endswith(".md") for name in names):
        raise ValueError(f"manifest contains a non-Markdown path: {path}")
    if len(names) != len(set(names)):
        raise ValueError(f"manifest contains duplicate filenames: {path}")
    return set(names)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-root", type=Path, default=ROOT / "Inputs" / "articles",
    )
    parser.add_argument(
        "--article-root", type=Path, default=ROOT / "entities" / "article",
    )
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--manifest-evidence", type=Path)
    parser.add_argument(
        "--manifest", type=Path,
        help="process only the direct input filenames listed in this frozen manifest",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_frozen_manifest(args.input_root.resolve())
    selected_names = load_manifest_names(args.manifest.resolve()) if args.manifest else None
    result = route_loose_articles(
        args.input_root.resolve(), args.article_root.resolve(), args.write, selected_names,
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.output.resolve().write_text(rendered + "\n", encoding="utf-8")
    if args.manifest_output:
        args.manifest_output.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.manifest_output.resolve().write_text(
            "\n".join(article["path"] for article in manifest["articles"]) + "\n",
            encoding="utf-8",
        )
    if args.manifest_evidence:
        args.manifest_evidence.resolve().parent.mkdir(parents=True, exist_ok=True)
        args.manifest_evidence.resolve().write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    print(rendered)
    return 1 if result["blocked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
