import datetime
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from tag_registry import (  # noqa: E402
    TagRecord,
    TagRegistryError,
    active_inventory,
    load_registry,
    registry_lookup,
    radar_status_at,
    status_at,
)
from optimize_tag_registry import OptimizationError, transition_text  # noqa: E402


def note(tag_id, display, status="active", history=None, aliases=None, radar_status="enabled"):
    history = history or [("2026-01-01T00:00:00+08:00", status)]
    aliases = aliases or []
    approved = status in {"active", "inactive", "deprecated"}
    rows = "\n".join(
        f"| {effective} | {value} | Reviewer | Evidence-backed decision. |"
        for effective, value in history
    )
    return f'''---
tagId: "{tag_id}"
displayName: "{display}"
aliases: {aliases!r}
status: {status}
statusEffectiveAt: "{history[-1][0]}"
radarStatus: {radar_status}
radarStatusEffectiveAt: "2026-01-01T00:00:00+08:00"
approvedAt: {('"2026-01-01T00:00:00+08:00"' if approved else 'null')}
approvedBy: {('"Reviewer"' if approved else 'null')}
articleCount: 0
---

# {display}

## Definition
Saved evidence.

## Replacement
- None — active tag.

## Status History
| Effective At | Status | Actor | Reason |
|---|---|---|---|
{rows}

## Radar Status History
| Effective At | Status | Actor | Reason |
|---|---|---|---|
| 2026-01-01T00:00:00+08:00 | {radar_status} | Reviewer | Evidence-backed decision. |

## Coverage

## Notes
'''


class TagEntityTests(unittest.TestCase):
    def test_active_inventory_excludes_non_active_tags(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "active.md").write_text(note("active", "Active"), encoding="utf-8")
            (root / "pending.md").write_text(
                note("pending", "Pending", "awaiting_approval"), encoding="utf-8"
            )
            self.assertEqual(active_inventory(root), [("Active", "active", 0)])

    def test_alias_collision_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            (root / "one.md").write_text(note("one", "One", aliases=["Shared"]), encoding="utf-8")
            (root / "two.md").write_text(note("two", "Two", aliases=["shared"]), encoding="utf-8")
            with self.assertRaises(TagRegistryError):
                registry_lookup(load_registry(root))

    def test_status_at_uses_effective_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            history = [
                ("2026-01-01T00:00:00+08:00", "active"),
                ("2026-07-01T00:00:00+08:00", "inactive"),
            ]
            (root / "tag.md").write_text(note("tag", "Tag", "inactive", history), encoding="utf-8")
            record = load_registry(root)[0]
            self.assertEqual(status_at(record, datetime.date(2026, 6, 30)), "active")
        self.assertEqual(status_at(record, datetime.date(2026, 7, 1)), "inactive")

    def test_radar_status_history_and_shadow_transition(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            path = root / "tag.md"
            path.write_text(note("tag", "Tag"), encoding="utf-8")
            record = load_registry(root)[0]
            self.assertEqual(radar_status_at(record, datetime.date(2026, 8, 18)), "enabled")
            updated = transition_text(
                path.read_text(encoding="utf-8"), "enabled", "shadow",
                "2026-08-18T17:00:00+08:00", "Reviewer", "Evidence ledger test.",
            )
            self.assertIn("radarStatus: shadow", updated)
            self.assertIn("| 2026-08-18T17:00:00+08:00 | shadow | Reviewer | Evidence ledger test. |", updated)
            with self.assertRaises(OptimizationError):
                transition_text(
                    path.read_text(encoding="utf-8"), "enabled", "disabled",
                    "2026-08-18T17:00:00+08:00", "Reviewer", "Invalid direct transition.",
                )

    def test_radar_rejects_unknown_database_tag_and_filters_inactive(self):
        spec = importlib.util.spec_from_file_location("tag_entity_radar", ROOT / "scripts" / "issue_radar.py")
        radar = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(radar)
        active = TagRecord(path=ROOT, tag_id="active", display_name="Active", aliases=(),
            status="active", status_effective_at="2026-01-01T00:00:00+08:00",
            radar_status="enabled", radar_status_effective_at="2026-01-01T00:00:00+08:00",
            approved_at="2026-01-01T00:00:00+08:00", approved_by="Reviewer", article_count=0,
            status_history=(), radar_status_history=(), body="")
        inactive = TagRecord(path=ROOT, tag_id="inactive", display_name="Inactive", aliases=(),
            status="inactive", status_effective_at="2026-01-01T00:00:00+08:00",
            radar_status="enabled", radar_status_effective_at="2026-01-01T00:00:00+08:00",
            approved_at="2026-01-01T00:00:00+08:00", approved_by="Reviewer", article_count=0,
            status_history=(), radar_status_history=(), body="")
        with mock.patch.object(radar, "status_at", side_effect=lambda record, _: record.status), \
             mock.patch.object(radar, "radar_status_at", side_effect=lambda record, _: record.radar_status):
            result = radar.apply_tag_registry(
                [{"id": 1, "tags": {"active", "inactive"}}],
                datetime.date(2026, 8, 14),
                [active, inactive],
            )
        self.assertEqual(result[0]["tags"], {"active"})
        with self.assertRaises(radar.RadarError):
            radar.apply_tag_registry(
                [{"id": 1, "tags": {"unknown"}}],
                datetime.date(2026, 8, 14),
                [active],
            )

    def test_radar_separates_enabled_shadow_and_disabled(self):
        spec = importlib.util.spec_from_file_location("tag_entity_radar_states", ROOT / "scripts" / "issue_radar.py")
        radar = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(radar)
        records = []
        for value in ("enabled", "shadow", "disabled"):
            records.append(TagRecord(path=ROOT, tag_id=value, display_name=value.title(), aliases=(),
                status="active", status_effective_at="2026-01-01T00:00:00+08:00",
                radar_status=value, radar_status_effective_at="2026-01-01T00:00:00+08:00",
                approved_at="2026-01-01T00:00:00+08:00", approved_by="Reviewer", article_count=0,
                status_history=(), radar_status_history=(), body=""))
        articles = [{"id": 1, "tags": {"Enabled", "Shadow", "Disabled"}}]
        with mock.patch.object(radar, "status_at", return_value="active"), \
             mock.patch.object(radar, "radar_status_at", side_effect=lambda record, _: record.radar_status):
            self.assertEqual(radar.apply_tag_registry(articles, datetime.date(2026, 8, 18), records)[0]["tags"], {"enabled"})
            self.assertEqual(radar.apply_tag_registry(articles, datetime.date(2026, 8, 18), records, {"shadow"})[0]["tags"], {"shadow"})

    def test_projector_rejects_unknown_and_non_active_tags(self):
        spec = importlib.util.spec_from_file_location(
            "tag_entity_projector", ROOT / "scripts" / "project_wiki_to_uat.py"
        )
        projector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(projector)
        active = TagRecord(path=ROOT, tag_id="active", display_name="Active", aliases=(),
            status="active", status_effective_at="2026-01-01T00:00:00+08:00",
            radar_status="enabled", radar_status_effective_at="2026-01-01T00:00:00+08:00",
            approved_at="2026-01-01T00:00:00+08:00", approved_by="Reviewer", article_count=0,
            status_history=(), radar_status_history=(), body="")
        inactive = TagRecord(path=ROOT, tag_id="inactive", display_name="Inactive", aliases=(),
            status="inactive", status_effective_at="2026-01-01T00:00:00+08:00",
            radar_status="enabled", radar_status_effective_at="2026-01-01T00:00:00+08:00",
            approved_at="2026-01-01T00:00:00+08:00", approved_by="Reviewer", article_count=0,
            status_history=(), radar_status_history=(), body="")
        with mock.patch.object(projector, "load_tag_registry", return_value=[active, inactive]):
            projector.validate_wiki_tag_assignments({
                "1": {"projection": {"tags": ["Active"]}}
            })
            with self.assertRaises(projector.ProjectionError):
                projector.validate_wiki_tag_assignments({
                    "1": {"projection": {"tags": ["Inactive"]}}
                })
            with self.assertRaises(projector.ProjectionError):
                projector.validate_wiki_tag_assignments({
                    "1": {"projection": {"tags": ["Unknown"]}}
                })


if __name__ == "__main__":
    unittest.main()
