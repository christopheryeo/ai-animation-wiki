import argparse
import datetime
import importlib.util
import json
import pathlib
import tempfile
import unittest
from unittest import mock


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "issue_radar.py"
SPEC = importlib.util.spec_from_file_location("issue_radar", SCRIPT)
RADAR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RADAR)


class IssueRadarTests(unittest.TestCase):
    def test_product_query_uses_read_only_canonical_uat_tables(self):
        query = RADAR.product_query("AI_Animation_UAT", "UAT_")
        self.assertIn("SET SESSION TRANSACTION READ ONLY", query)
        self.assertIn("`AI_Animation_UAT`.`UAT_articles`", query)
        self.assertIn("`AI_Animation_UAT`.`UAT_article_tags`", query)
        self.assertIn("`AI_Animation_UAT`.`UAT_article_coverage`", query)

    def test_product_query_rejects_unsafe_identifiers(self):
        with self.assertRaises(RADAR.RadarError):
            RADAR.product_query("AI_Animation; DROP DATABASE x", "")

    def test_parse_product_rows_normalises_tags_and_deduplicates_coverage(self):
        row = {
            "article_id": 42,
            "title": "Example",
            "published_date": "2026-07-22T12:30:00",
            "category": "Studio Sector",
            "tone": "Opinionated",
            "event_type": "Unfacilitated",
            "tags": [" Enlistment Act ", "ENLISTMENT ACT", None],
            "outlets": ["CNA", "CNA", None],
            "countries": ["Southeast Asia", "Southeast Asia", None],
        }
        articles = RADAR.parse_product_rows(json.dumps(row))
        self.assertEqual(articles[0]["tags"], {"enlistment act"})
        self.assertEqual(articles[0]["outlets"], {"CNA"})
        self.assertEqual(articles[0]["countries"], {"Southeast Asia"})
        self.assertTrue(articles[0]["unfac"])
        self.assertTrue(articles[0]["opin"])

    def test_historical_candidates_do_not_use_future_articles(self):
        asof = datetime.date(2026, 1, 31)
        articles = []
        for index in range(7):
            articles.append(self.article(asof - datetime.timedelta(days=index * 7), "future-assisted"))
        for index in range(3):
            articles.append(self.article(asof + datetime.timedelta(days=index + 1), "future-assisted"))
        self.assertNotIn("future-assisted", RADAR.candidates(articles, asof))

    def test_mysql_defaults_file_is_first_client_option(self):
        args = argparse.Namespace(
            mysql_program="mysql", defaults_file="/secure/client.cnf", login_path=None,
            mysql_host=None, mysql_port=None, mysql_user=None, ssl_mode=None,
        )
        command = RADAR.mysql_command(args)
        self.assertEqual(command[:2], ["mysql", "--defaults-extra-file=/secure/client.cnf"])

    def test_production_source_specific_credentials_override_generic_values(self):
        args = argparse.Namespace(
            source="production",
            mysql_program="mysql",
            defaults_file=None,
            login_path=None,
            mysql_host="generic-host",
            mysql_port=3306,
            mysql_user="generic-user",
            ssl_mode=None,
        )
        environment = {
            "ISSUE_RADAR_PRODUCTION_MYSQL_HOST": "reader-host",
            "ISSUE_RADAR_PRODUCTION_MYSQL_PORT": "3307",
            "ISSUE_RADAR_PRODUCTION_MYSQL_USER": "issue_radar_reader",
        }
        with mock.patch.dict(RADAR.os.environ, environment, clear=False):
            command = RADAR.mysql_command(args)
        self.assertIn("reader-host", command)
        self.assertIn("3307", command)
        self.assertIn("issue_radar_reader", command)
        self.assertNotIn("generic-host", command)
        self.assertNotIn("generic-user", command)

    def test_production_query_uses_unprefixed_product_tables(self):
        database, prefix = RADAR.SOURCE_DEFAULTS["production"]
        query = RADAR.product_query(database, prefix)
        self.assertIn("`AI_Animation`.`articles`", query)
        self.assertIn("`AI_Animation`.`article_tags`", query)
        self.assertIn("`AI_Animation`.`article_coverage`", query)
        self.assertNotIn("`UAT_", query)

    def test_secure_password_prompt_option_is_available(self):
        args = RADAR.build_parser().parse_args(["--prompt-password"])
        self.assertTrue(args.prompt_password)

    def test_tag_inventory_query_is_read_only_and_counts_articles(self):
        query = RADAR.product_tags_query("AI_Animation", "")
        self.assertIn("SET SESSION TRANSACTION READ ONLY", query)
        self.assertIn("COUNT(DISTINCT article_id)", query)
        self.assertIn("GROUP BY BINARY tag", query)
        self.assertIn("`AI_Animation`.`article_tags`", query)

    def test_candidate_requires_eight_articles_across_three_weeks(self):
        asof = datetime.date(2026, 7, 31)
        articles = self.filler_articles(asof, 300)
        for index in range(8):
            articles.append(self.article(asof - datetime.timedelta(days=index * 4), "eligible"))
        self.assertIn("eligible", RADAR.candidates(articles, asof))

        too_few = self.filler_articles(asof, 300)
        for index in range(7):
            too_few.append(self.article(asof - datetime.timedelta(days=index * 4), "too-few"))
        self.assertNotIn("too-few", RADAR.candidates(too_few, asof))

        one_week = self.filler_articles(asof, 300)
        for index in range(8):
            one_week.append(self.article(asof - datetime.timedelta(days=index % 3), "one-week"))
        self.assertNotIn("one-week", RADAR.candidates(one_week, asof))

    def test_candidate_excludes_generic_and_stop_tags(self):
        asof = datetime.date(2026, 7, 31)
        articles = self.filler_articles(asof, 300)
        for index in range(10):
            day = asof - datetime.timedelta(days=index * 3)
            articles.append(self.article(day, "too-generic"))
        for index in range(8):
            day = asof - datetime.timedelta(days=index * 4)
            articles.append(self.article(day, "animation"))
        candidates = RADAR.candidates(articles, asof)
        self.assertNotIn("too-generic", candidates)
        self.assertNotIn("animation", candidates)

    def test_wave_runs_require_more_than_two_weeks_of_separation(self):
        asof = datetime.date(2026, 7, 31)
        dates = [
            datetime.date(2026, 5, 4),
            datetime.date(2026, 5, 18),
            datetime.date(2026, 6, 8),
            datetime.date(2026, 7, 6),
        ]
        self.assertEqual(RADAR.waves(dates, asof), 3)

    def test_score_components_cover_all_six_signals(self):
        asof = datetime.date(2026, 7, 31)
        selected = [
            self.article(
                datetime.date(2026, 5, 1), "signal", category="Other",
                outlets={"old"}, countries={"Southeast Asia"},
            ),
            self.article(
                datetime.date(2026, 6, 20), "signal", category="Other",
                outlets={"old"}, countries={"Southeast Asia"},
            ),
        ]
        for index in range(4):
            selected.append(self.article(
                asof - datetime.timedelta(days=index), "signal",
                category="Animation Studio",
                outlets={f"new-{index}"},
                countries={f"country-{index}"},
                unfac=True,
                opin=index < 2,
            ))
        result = RADAR.score_issue(selected, asof)
        self.assertAlmostEqual(result["parts"]["accel"], 1.0)
        self.assertAlmostEqual(result["parts"]["breadth"], 1.0)
        self.assertAlmostEqual(result["parts"]["inst"], 1.0)
        self.assertAlmostEqual(result["parts"]["recur"], 2 / 3)
        self.assertAlmostEqual(result["parts"]["unfac"], 1.0)
        self.assertAlmostEqual(result["parts"]["opin"], 0.5)

    def test_window_boundaries_are_hindsight_free(self):
        asof = datetime.date(2026, 7, 31)
        selected = [
            self.article(datetime.date(2026, 6, 5), "boundary"),
            self.article(datetime.date(2026, 7, 3), "boundary"),
            self.article(datetime.date(2026, 7, 4), "boundary"),
            self.article(datetime.date(2026, 7, 30), "boundary"),
            self.article(datetime.date(2026, 7, 31), "boundary"),
            self.article(datetime.date(2026, 8, 1), "boundary"),
        ]
        result = RADAR.score_issue(selected, asof)
        self.assertEqual(result["vol"], 3)
        self.assertIn("volume 1->3 over two 28d windows", result["why"])

    def test_tier_boundaries_require_score_and_volume(self):
        self.assertEqual(RADAR.classify_tier(0.60, 8), "HOT")
        self.assertEqual(RADAR.classify_tier(0.59, 8), "WARM")
        self.assertEqual(RADAR.classify_tier(0.40, 4), "WARM")
        self.assertEqual(RADAR.classify_tier(0.39, 4), "WATCH")
        self.assertEqual(RADAR.classify_tier(0.25, 2), "WATCH")
        self.assertIsNone(RADAR.classify_tier(0.60, 1))

    def test_missing_optional_values_parse_as_empty_sets(self):
        row = {
            "article_id": 7,
            "title": None,
            "published_date": "2026-07-31T00:00:00",
            "category": None,
            "tone": None,
            "event_type": None,
            "tags": None,
            "outlets": None,
            "countries": None,
        }
        article = RADAR.parse_product_rows(json.dumps(row))[0]
        self.assertEqual(article["tags"], set())
        self.assertEqual(article["outlets"], set())
        self.assertEqual(article["countries"], set())
        self.assertFalse(article["unfac"])
        self.assertFalse(article["opin"])

    def test_structured_output_is_complete_and_stable(self):
        asof = datetime.date(2026, 7, 31)
        result = {
            "score": 0.5,
            "tier": "WARM",
            "vol": 4,
            "parts": {
                "accel": 1.0, "breadth": 0.5, "inst": 0.4,
                "recur": 0.3, "unfac": 0.2, "opin": 0.1,
            },
            "why": ["example reason"],
        }
        articles = [self.article(asof, "example")]
        payload = RADAR.structured_run(
            articles, "AI_Animation_UAT", "UAT_", asof, 1, [("example", result)], "WATCH",
        )
        first = json.dumps(payload, sort_keys=True)
        second = json.dumps(
            RADAR.structured_run(
                articles, "AI_Animation_UAT", "UAT_", asof, 1, [("example", result)], "WATCH",
            ),
            sort_keys=True,
        )
        self.assertEqual(first, second)
        self.assertEqual(set(payload["flags"][0]["signals"]), set(RADAR.WEIGHTS))
        self.assertEqual(payload["flags"][0]["articleIds"], [])
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "nested" / "radar.json"
            RADAR.write_structured_run(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)

    def test_structured_output_carries_supporting_article_ids(self):
        asof = datetime.date(2026, 7, 31)
        recent = self.article(asof, "example")
        recent["id"] = 2
        old = self.article(datetime.date(2026, 5, 1), "example")
        old["id"] = 1
        result = {
            "score": 0.5,
            "tier": "WARM",
            "vol": 1,
            "parts": dict.fromkeys(RADAR.WEIGHTS, 0.0),
            "why": [],
        }
        payload = RADAR.structured_run(
            [old, recent], "AI_Animation_UAT", "UAT_", asof, 1,
            [("example", result)], "WATCH", {"example": [recent, old]},
        )
        self.assertEqual(payload["flags"][0]["articleIds"], [1, 2])
        self.assertEqual(payload["flags"][0]["recentArticleIds"], [2])

    def test_ranked_report_is_stable_and_writable(self):
        asof = datetime.date(2026, 7, 31)
        result = {
            "score": 0.5,
            "tier": "WARM",
            "vol": 4,
            "parts": dict.fromkeys(RADAR.WEIGHTS, 0.0),
            "why": ["example reason"],
        }
        report = RADAR.ranked_report(
            asof, [("example", result)], 100, 12, "AI_Animation_UAT.UAT_articles",
        )
        self.assertIn("[WARM ] 0.50  example", report)
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "nested" / "radar.txt"
            RADAR.write_text_output(path, report)
            self.assertEqual(path.read_text(encoding="utf-8"), report)

    @staticmethod
    def article(
        day, tag, category="", outlets=None, countries=None, unfac=False, opin=False,
    ):
        return {
            "id": 1, "title": "", "date": day, "cat": category, "tags": {tag},
            "outlets": set(outlets or ()), "countries": set(countries or ()),
            "unfac": unfac, "opin": opin,
        }

    @classmethod
    def filler_articles(cls, asof, count):
        return [
            {
                **cls.article(asof - datetime.timedelta(days=index % 60), ""),
                "tags": set(),
                "id": index + 1000,
            }
            for index in range(count)
        ]


if __name__ == "__main__":
    unittest.main()
