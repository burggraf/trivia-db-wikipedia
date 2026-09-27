#!/usr/bin/env python3
"""Create an editable, read-only-source shortlist of popular Wikipedia articles."""

import argparse
import csv
import sqlite3
from contextlib import closing
from pathlib import Path

POLICY_VERSION = "us-pageviews-10y-top-n-v1"
DEFAULT_DB = Path("data/wikipedia.sqlite")
DEFAULT_OUTPUT = Path("data/trivia_candidates.csv")
ARTICLE_BATCH_SIZE = 900
CSV_FIELDS = (
    "rank",
    "page_id",
    "title",
    "wikidata_qid",
    "description",
    "abstract",
    "abstract_word_count",
    "question_count_ceiling",
    "days_observed",
    "dp_views_sum",
    "popularity_window_start",
    "popularity_window_end",
    "source_revision_id",
    "source_modified_at",
    "url",
    "license_json",
    "review_flags",
    "include_in_pilot",
    "question_count_override",
    "review_notes",
    "selection_policy_version",
)
ARTICLE_COLUMNS = (
    "page_id, title, wikidata_qid, description, abstract, url, revision_id, "
    "modified_at, license_json"
)


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("limit must be positive")
    return parsed


def question_count_ceiling(word_count):
    if word_count < 350:
        return 1
    if word_count < 550:
        return 2
    return 4


def connect_readonly(db_path):
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def load_candidates(conn, limit):
    popularity_rows = conn.execute(
        """SELECT page_id, window_start, window_end, days_observed, dp_views_sum
           FROM us_page_popularity_10y
           ORDER BY dp_views_sum DESC, page_id ASC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    if not popularity_rows:
        return []

    articles = {}
    page_ids = [row["page_id"] for row in popularity_rows]
    for start in range(0, len(page_ids), ARTICLE_BATCH_SIZE):
        batch = page_ids[start:start + ARTICLE_BATCH_SIZE]
        placeholders = ",".join("?" for _ in batch)
        for article in conn.execute(
            f"SELECT {ARTICLE_COLUMNS} FROM articles WHERE page_id IN ({placeholders})",
            batch,
        ):
            articles[article["page_id"]] = article

    missing = [page_id for page_id in page_ids if page_id not in articles]
    if missing:
        sample = ", ".join(str(page_id) for page_id in missing[:10])
        raise ValueError(f"Popularity table has page_id values missing articles: {sample}")

    candidates = []
    for rank, popularity in enumerate(popularity_rows, 1):
        article = articles[popularity["page_id"]]
        word_count = len(article["abstract"].split())
        flags = []
        if word_count < 80:
            flags.append("short_abstract")
        if not (article["description"] or "").strip():
            flags.append("missing_description")
        candidates.append({
            "rank": rank,
            "page_id": article["page_id"],
            "title": article["title"],
            "wikidata_qid": article["wikidata_qid"],
            "description": article["description"],
            "abstract": article["abstract"],
            "abstract_word_count": word_count,
            "question_count_ceiling": question_count_ceiling(word_count),
            "days_observed": popularity["days_observed"],
            "dp_views_sum": popularity["dp_views_sum"],
            "popularity_window_start": popularity["window_start"],
            "popularity_window_end": popularity["window_end"],
            "source_revision_id": article["revision_id"],
            "source_modified_at": article["modified_at"],
            "url": article["url"],
            "license_json": article["license_json"],
            "review_flags": ";".join(flags),
            "include_in_pilot": 0,
            "question_count_override": "",
            "review_notes": "",
            "selection_policy_version": POLICY_VERSION,
        })
    return candidates


def write_manifest(path, candidates, force=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "w" if force else "x"
    with path.open(mode, newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS, extrasaction="raise")
        writer.writeheader()
        writer.writerows(candidates)


def report(candidates, output):
    print(f"Selection policy: {POLICY_VERSION}")
    print(f"Candidates: {len(candidates)}")
    if candidates:
        windows = sorted({
            (row["popularity_window_start"], row["popularity_window_end"])
            for row in candidates
        })
        print("Popularity windows: " + "; ".join(f"{start} to {end}" for start, end in windows))
        bands = ((1, 100), (101, 1000), (1001, 5000), (5001, 10000))
        if candidates[-1]["rank"] > 10000:
            bands += ((10001, candidates[-1]["rank"]),)
        print("Rank bands: " + ", ".join(
            f"{low}-{high}={sum(low <= row['rank'] <= high for row in candidates)}"
            for low, high in bands
        ))
        word_bands = (
            ("<80", lambda count: count < 80),
            ("80-349", lambda count: 80 <= count < 350),
            ("350-549", lambda count: 350 <= count < 550),
            ("550+", lambda count: count >= 550),
        )
        print("Abstract word-count bands: " + ", ".join(
            f"{name}={sum(test(row['abstract_word_count']) for row in candidates)}"
            for name, test in word_bands
        ))
        ceilings = sorted({row["question_count_ceiling"] for row in candidates})
        print("Question-count ceilings: " + ", ".join(
            f"{count}={sum(row['question_count_ceiling'] == count for row in candidates)}"
            for count in ceilings
        ))
        print(f"Short abstracts (<80 words): {sum('short_abstract' in row['review_flags'] for row in candidates)}")
        print(f"Missing descriptions: {sum('missing_description' in row['review_flags'] for row in candidates)}")
    print("Categories are not assigned at article level; classify each generated question.")
    print("Set include_in_pilot=1 to select articles; question_count_override is informational only in the current one-question-per-page generator.")
    print(f"Editable manifest: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=positive_int, default=10000)
    parser.add_argument("--force", action="store_true", help="overwrite an existing manifest")
    args = parser.parse_args()

    db_path = args.db.resolve()
    output_path = args.output.resolve()
    if not db_path.is_file():
        parser.error(f"SQLite database not found: {db_path}")
    if output_path == db_path:
        parser.error("output cannot be the source database")
    if output_path.exists() and not args.force:
        parser.error(f"{output_path} already exists; refusing to overwrite an edited manifest (use --force to replace it)")

    try:
        with closing(connect_readonly(db_path)) as conn:
            candidates = load_candidates(conn, args.limit)
        write_manifest(output_path, candidates, force=args.force)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    report(candidates, output_path)


if __name__ == "__main__":
    main()
