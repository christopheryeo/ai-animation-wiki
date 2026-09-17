#!/usr/bin/env python3
"""Shared loader for the Git-ignored ``.env.local`` file.

Any script that needs a local secret or connection setting not meant for git
(API keys, MySQL connection details) should import ``load_local_env`` from
here rather than re-implementing its own copy. Values already present in the
process environment always win — ``.env.local`` only fills gaps, so an
operator's real ``export`` still overrides the file.

Registered by [[load-shared-config-from-env-local]]. Originally introduced as
a private helper in ``enrich_radar_inputs.py`` for ``OPENAI_API_KEY`` only;
factored out here so ``issue_radar.py`` (and any future script) can read
``ISSUE_RADAR_MYSQL_*`` settings from the same file without a second copy of
the parsing logic.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = ROOT / ".env.local"


def load_local_env(path: Path = DEFAULT_ENV_FILE) -> None:
    """Load simple ``KEY=VALUE`` entries without overriding the environment."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
