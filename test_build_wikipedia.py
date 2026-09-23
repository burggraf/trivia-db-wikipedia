import sqlite3
import tempfile
import unittest
from pathlib import Path

import build_wikipedia
from build_wikipedia import ensure_schema, import_rows, row_from_json


class ImportRowsTest(unittest.TestCase):
    def test_maps_duckdb_json_row_to_sqlite_column_order(self):
        row = row_from_json(
            '{"page_id":1,"title":"Title","wikidata_qid":"Q1",'
            '"description":"desc","abstract":"text","url":"https://example/1",'
            '"revision_id":2,"modified_at":"2026-01-01",'
            '"license_json":[{"identifier":"CC-BY-SA-4.0"}]}'
        )
        self.assertEqual(row, (
            1, "Title", "Q1", "desc", "text", "https://example/1", 2,
            "2026-01-01", '[{"identifier":"CC-BY-SA-4.0"}]',
        ))

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.conn = sqlite3.connect(Path(self.temp_dir.name) / "test.sqlite")
        self.addCleanup(self.conn.close)
        ensure_schema(self.conn)

    def test_skips_missing_or_blank_abstracts(self):
        rows = [
            (1, "No abstract", None, None, None, "https://example/1", 1, None, None),
            (2, "Blank", None, None, "  \n", "https://example/2", 2, None, None),
            (3, "Valid", "Q3", "desc", "Useful abstract. " * 12, "https://example/3", 3, "2026-01-01", "[]"),
        ]

        count = import_rows(self.conn, rows, batch_size=2)
        self.conn.commit()

        self.assertEqual(count, 1)
        self.assertEqual(self.conn.execute("SELECT page_id, title, abstract FROM articles").fetchall(), [
            (3, "Valid", "Useful abstract. " * 12)
        ])

    def test_filters_short_and_disambiguation_abstracts(self):
        fact = "A useful fact. " + "It gives specific details about the subject. " * 5
        rows = [
            (8, "Short", None, None, "Too short.", "url", 1, None, None),
            (9, "111th", None, None, "111th may refer to:\n" + "- A listed item\n" * 20, "url", 1, None, None),
            (10, "18 Squadron", None, None, "18 Squadron may also refer to:\n" + "- A listed item\n" * 20, "url", 1, None, None),
            (11, "Thing (disambiguation)", None, None, fact, "url", 1, None, None),
            (12, "Useful subject", None, None, fact, "url", 1, None, None),
        ]

        count = import_rows(self.conn, rows)
        self.conn.commit()

        self.assertEqual(count, 1)
        self.assertEqual(self.conn.execute("SELECT page_id FROM articles").fetchall(), [(12,)])

    def test_prunes_existing_rows_that_fail_the_filter(self):
        fact = "A useful fact. " + "It gives specific details about the subject. " * 5
        rows = [
            (20, "Short", None, None, "Too short.", "url", 1, None, None),
            (21, "111th", None, None, "111th may refer to:\n" + "- A listed item\n" * 20, "url", 1, None, None),
            (22, "Useful subject", None, None, fact, "url", 1, None, None),
        ]
        self.conn.executemany(
            "INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        self.conn.commit()

        removed = build_wikipedia.prune_existing_rows(self.conn)

        self.assertEqual(removed, 2)
        self.assertEqual(self.conn.execute("SELECT page_id FROM articles").fetchall(), [(22,)])

    def test_keeps_highest_revision_for_duplicate_page_id(self):
        rows = [
            (7, "Page", None, None, "Newer fact. " * 20, "https://example/7", 12, None, None),
            (7, "Page", None, None, "Older fact. " * 20, "https://example/7", 11, None, None),
        ]

        import_rows(self.conn, rows)
        self.conn.commit()

        self.assertEqual(
            self.conn.execute("SELECT abstract, revision_id FROM articles WHERE page_id = 7").fetchone(),
            ("Newer fact. " * 20, 12),
        )


if __name__ == "__main__":
    unittest.main()
