import sqlite3
import unittest
from datetime import date

from build_wikipedia import ensure_schema
from import_us_pageviews_10y import (
    completed_months, dataset_url, ensure_stage, finalize_summary,
    initialize_stage, match_observations, parse_dataset_row, stage_window,
    store_month,
)


class TenYearPageviewsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO articles (page_id, title, revision_id, abstract) VALUES (?, ?, ?, ?)",
            [(1, "Test page", 10, "older"), (2, "Test page", 20, "newer")],
        )

    def test_dataset_paths_follow_release_periods(self):
        self.assertIn(
            "country_project_page_historical_pre_2017/2016-09-23.tsv",
            dataset_url(date(2016, 9, 23)),
        )
        self.assertIn(
            "country_project_page_historical/2017-02-09.tsv",
            dataset_url(date(2017, 2, 9)),
        )
        self.assertIn(
            "country_project_page/2023-02-06.tsv",
            dataset_url(date(2023, 2, 6)),
        )

    def test_parses_pre_and_post_2017_rows_and_filters_project_country(self):
        self.assertEqual(
            parse_dataset_row("United States of America\tUS\ten.wikipedia\t\tTest_page\t4200"),
            (None, "Test_page", 4200),
        )
        self.assertEqual(
            parse_dataset_row("United States of America\tUS\ten.wikipedia\t2\tTest_page\tQ1\t900"),
            (2, "Test_page", 900),
        )
        self.assertIsNone(parse_dataset_row("Canada\tCA\ten.wikipedia\t2\tTest_page\tQ1\t900"))
        self.assertIsNone(parse_dataset_row("United States of America\tUS\tfr.wikipedia\t2\tTest_page\tQ1\t900"))

    def test_matches_page_ids_and_legacy_titles(self):
        observations, unmatched = match_observations(
            self.conn,
            [(2, "Test_page", 500), (None, "Test_page", 700), (999, "Missing", 100)],
        )
        self.assertEqual(observations, [(2, 700)])
        self.assertEqual(unmatched, 1)

    def test_monthly_checkpoints_are_idempotent_and_finalize_summary(self):
        ensure_stage(self.conn)
        initialize_stage(self.conn, "2016-09-23", "2026-09-22")
        self.assertEqual(stage_window(self.conn), ("2016-09-23", "2026-09-22"))
        self.assertTrue(store_month(self.conn, "2026-08", {
            1: {"days_observed": 1, "dp_views_sum": 700,
                "first_seen": "2026-08-21", "last_seen": "2026-08-21"},
        }, 0, 0))
        self.assertFalse(store_month(self.conn, "2026-08", {}, 0, 0))
        self.assertTrue(store_month(self.conn, "2026-09", {
            2: {"days_observed": 1, "dp_views_sum": 1000,
                "first_seen": "2026-09-22", "last_seen": "2026-09-22"},
        }, 1, 1))
        self.assertEqual(len(completed_months(self.conn)), 2)

        self.assertEqual(finalize_summary(self.conn), (2, 1, 1))
        self.assertEqual(
            self.conn.execute("""
                SELECT page_id, window_start, window_end, days_observed,
                       dp_views_sum, first_seen, last_seen
                FROM us_page_popularity_10y ORDER BY page_id
            """).fetchall(),
            [
                (1, "2016-09-23", "2026-09-22", 1, 700, "2026-08-21", "2026-08-21"),
                (2, "2016-09-23", "2026-09-22", 1, 1000, "2026-09-22", "2026-09-22"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
