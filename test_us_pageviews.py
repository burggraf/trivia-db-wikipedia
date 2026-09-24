import io
import sqlite3
import unittest
from datetime import date
from unittest.mock import patch

from build_wikipedia import ensure_schema
from import_us_pageviews import (
    collect_day, date_window, fetch_day, summary_table, write_summary,
)


class USPageviewsTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)
        self.conn.executemany(
            "INSERT INTO articles (page_id, title, revision_id, abstract) VALUES (?, ?, ?, ?)",
            [
                (1, "Test page", 10, "older revision"),
                (2, "Test page", 20, "newer revision"),
            ],
        )

    def test_retries_transient_socket_timeout(self):
        with patch(
            "import_us_pageviews.urlopen",
            side_effect=[TimeoutError("timed out"), io.BytesIO(b'{"items":[]}')],
        ) as open_url, patch("import_us_pageviews.time.sleep"):
            payload = fetch_day(date(2026, 9, 20))

        self.assertEqual(payload, {"items": []})
        self.assertEqual(open_url.call_count, 2)

    def test_365_day_window_includes_both_endpoints(self):
        self.assertEqual(
            date_window(date(2026, 9, 20)),
            (date(2025, 9, 21), date(2026, 9, 20)),
        )

    def test_period_summaries_use_separate_tables(self):
        self.assertEqual(summary_table(365), "us_page_popularity_365d")
        self.assertEqual(summary_table(1826), "us_page_popularity_5y")

        write_summary(
            self.conn, {1: {
                "days_in_top_1000": 1,
                "observed_views_ceil_sum": 1100,
                "best_rank": 3,
                "last_seen": "2026-09-22",
            }}, "2025-09-23", "2026-09-22", summary_table(365),
        )
        write_summary(
            self.conn, {2: {
                "days_in_top_1000": 1,
                "observed_views_ceil_sum": 1200,
                "best_rank": 2,
                "last_seen": "2026-09-22",
            }}, "2021-09-23", "2026-09-22", summary_table(1826),
        )
        self.assertEqual(self.conn.execute(
            "SELECT page_id FROM us_page_popularity_365d"
        ).fetchall(), [(1,)])
        self.assertEqual(self.conn.execute(
            "SELECT page_id FROM us_page_popularity_5y"
        ).fetchall(), [(2,)])

    def test_aggregates_us_english_views_and_joins_latest_title_revision(self):
        stats = {}
        first_day = {
            "items": [{
                "country": "US",
                "articles": [
                    {"article": "Test_page", "project": "en.wikipedia", "views_ceil": 1300, "rank": 4},
                    {"article": "Test_page", "project": "fr.wikipedia", "views_ceil": 5000, "rank": 1},
                    {"article": "Missing_Page", "project": "en.wikipedia", "views_ceil": 1200, "rank": 5},
                ],
            }]
        }
        second_day = {
            "items": [{
                "country": "US",
                "articles": [
                    {"article": "Test_page", "project": "en.wikipedia", "views_ceil": 1200, "rank": 7},
                ],
            }]
        }

        self.assertEqual(collect_day(self.conn, "2026-09-19", first_day, stats), (1, 1))
        self.assertEqual(collect_day(self.conn, "2026-09-20", second_day, stats), (1, 0))
        write_summary(self.conn, stats, "2025-09-21", "2026-09-20")

        row = self.conn.execute("""
            SELECT page_id, window_start, window_end, days_in_top_1000,
                   observed_views_ceil_sum, best_rank, last_seen
            FROM us_page_popularity_365d
        """).fetchone()
        self.assertEqual(row, (2, "2025-09-21", "2026-09-20", 2, 2500, 4, "2026-09-20"))


if __name__ == "__main__":
    unittest.main()
