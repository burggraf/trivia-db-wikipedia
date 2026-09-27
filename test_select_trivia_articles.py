import ast
import csv
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("select_trivia_articles.py")


class SelectTriviaArticlesTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.directory = Path(self.temp_dir.name)
        self.db_path = self.directory / "wikipedia.sqlite"
        self.output_path = self.directory / "candidates.csv"
        self.create_database()

    def create_database(self):
        conn = sqlite3.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE articles (
                page_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                wikidata_qid TEXT,
                description TEXT,
                abstract TEXT NOT NULL,
                url TEXT,
                revision_id INTEGER,
                modified_at TEXT,
                license_json TEXT
            );
            CREATE TABLE us_page_popularity_10y (
                page_id INTEGER PRIMARY KEY REFERENCES articles(page_id),
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                days_observed INTEGER NOT NULL,
                dp_views_sum INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
        """)
        articles = [
            (1, "Short & missing description", "Q1", None, " ".join(["short"] * 70),
             "https://example.test/1", 101, "2025-01-01", '[{"name":"CC BY-SA"}]'),
            (2, "Broad article", "Q2", "A broad topic", " ".join(["broad"] * 400),
             "https://example.test/2", 202, "2025-02-02", None),
            (3, "Long article", "Q3", "A long topic", "\n".join(["long"] * 600),
             "https://example.test/3", 303, "2025-03-03", None),
            (4, "Medium article", "Q4", "A medium topic", " ".join(["medium"] * 200),
             "https://example.test/4", 404, "2025-04-04", None),
        ]
        conn.executemany("INSERT INTO articles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", articles)
        popularity = [
            (1, "2016-09-23", "2026-09-22", 100, 100, "2016-09-23", "2026-09-22"),
            (2, "2016-09-23", "2026-09-22", 200, 100, "2016-09-23", "2026-09-22"),
            (3, "2016-09-23", "2026-09-22", 300, 1000, "2016-09-23", "2026-09-22"),
            (4, "2016-09-23", "2026-09-22", 400, 50, "2016-09-23", "2026-09-22"),
        ]
        conn.executemany("INSERT INTO us_page_popularity_10y VALUES (?, ?, ?, ?, ?, ?, ?)", popularity)
        conn.commit()

    def run_selector(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.db_path),
             "--output", str(self.output_path), *args],
            text=True,
            capture_output=True,
        )

    def test_selector_parses_with_python_311_grammar(self):
        ast.parse(SCRIPT.read_text(encoding='utf-8'), feature_version=(3, 11))

    def test_writes_ranked_editable_manifest_with_length_based_ceilings(self):
        result = self.run_selector("--limit", "4")
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.output_path.open(newline="", encoding="utf-8") as file:
            rows = list(csv.DictReader(file))

        self.assertEqual([row["page_id"] for row in rows], ["3", "1", "2", "4"])
        self.assertEqual([row["rank"] for row in rows], ["1", "2", "3", "4"])
        self.assertEqual([row["abstract_word_count"] for row in rows], ["600", "70", "400", "200"])
        self.assertEqual([row["question_count_ceiling"] for row in rows], ["4", "1", "2", "1"])
        self.assertEqual(rows[1]["review_flags"], "short_abstract;missing_description")
        self.assertEqual(rows[1]["include_in_pilot"], "0")
        self.assertEqual(rows[1]["question_count_override"], "")
        self.assertEqual(rows[1]["source_revision_id"], "101")
        self.assertEqual(rows[0]["abstract"], "\n".join(["long"] * 600))
        self.assertIn("Candidates: 4", result.stdout)
        self.assertIn("Question-count ceilings: 1=2, 2=1, 4=1", result.stdout)
        self.assertIn("question_count_override is informational only", result.stdout)
        self.assertIn("Categories are not assigned at article level", result.stdout)

    def test_refuses_to_overwrite_an_edited_manifest_without_force(self):
        first = self.run_selector("--limit", "2")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.output_path.write_text("manual selection edits", encoding="utf-8")

        second = self.run_selector("--limit", "3")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr)
        self.assertEqual(self.output_path.read_text(encoding="utf-8"), "manual selection edits")

    def test_rejects_nonpositive_limit_without_creating_manifest(self):
        result = self.run_selector("--limit", "0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("limit must be positive", result.stderr)
        self.assertFalse(self.output_path.exists())

    def test_rejects_popularity_rows_without_matching_articles(self):
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                "INSERT INTO us_page_popularity_10y VALUES (?, ?, ?, ?, ?, ?, ?)",
                (999, "2016-09-23", "2026-09-22", 1, 9999, "2016-09-23", "2016-09-23"),
            )
            conn.commit()

        result = self.run_selector("--limit", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing articles", result.stderr)
        self.assertFalse(self.output_path.exists())

    def test_refuses_source_database_as_output_even_with_force(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.db_path),
             "--output", str(self.db_path), "--force"],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("source database", result.stderr)
        self.assertTrue(self.db_path.is_file())


if __name__ == "__main__":
    unittest.main()
