#!/usr/bin/env python3
"""Aggregate ten years of Wikimedia's differentially private U.S. pageviews."""

import argparse
import sqlite3
import time
from datetime import date, timedelta
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from import_us_pageviews import USER_AGENT, date_window, lookup_page_ids, normalize_title

BASE_URL = "https://analytics.wikimedia.org/published/datasets"
HISTORICAL_START = date(2017, 2, 9)
CURRENT_START = date(2023, 2, 6)
WINDOW_DAYS = 3652
SUMMARY = "us_page_popularity_10y"
STAGE = "_us_page_popularity_10y_stage"
STAGE_MONTHS = "_us_page_popularity_10y_months"
STAGE_WINDOW = "_us_page_popularity_10y_window"


def dataset_url(day):
    if day < HISTORICAL_START:
        dataset = "country_project_page_historical_pre_2017"
    elif day < CURRENT_START:
        dataset = "country_project_page_historical"
    else:
        dataset = "country_project_page"
    return f"{BASE_URL}/{dataset}/{day:%Y-%m-%d}.tsv"


def parse_dataset_row(line):
    fields = line.rstrip("\r\n").split("\t")
    if len(fields) < 6 or fields[1] != "US" or fields[2] != "en.wikipedia":
        return None
    page_id = int(fields[3]) if fields[3].isdigit() else None
    return page_id, fields[4], int(fields[-1])


def fetch_day_rows(day):
    request = Request(
        dataset_url(day),
        headers={"User-Agent": USER_AGENT, "Accept": "text/tab-separated-values"},
    )
    for attempt in range(4):
        try:
            rows = []
            with urlopen(request, timeout=60) as response:
                for raw_line in response:
                    row = parse_dataset_row(raw_line.decode("utf-8", errors="replace"))
                    if row is not None:
                        rows.append(row)
            return rows
        except HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                raise
            try:
                delay = float(exc.headers.get("Retry-After", 2 ** attempt))
            except (TypeError, ValueError):
                delay = 2 ** attempt
            time.sleep(delay)
        except (URLError, TimeoutError, OSError, IncompleteRead):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return None


def latest_available_day(today=None, lookback=7):
    today = today or date.today()
    for days_ago in range(lookback + 1):
        day = today - timedelta(days=days_ago)
        rows = fetch_day_rows(day)
        if rows:
            return day, rows
    raise RuntimeError(f"No U.S. pageview files found in the last {lookback + 1} days")


def existing_page_ids(conn, ids):
    ids = list(set(ids))
    result = set()
    for start in range(0, len(ids), 900):
        batch = ids[start:start + 900]
        placeholders = ",".join("?" for _ in batch)
        result.update(
            row[0] for row in conn.execute(
                f"SELECT page_id FROM articles WHERE page_id IN ({placeholders})", batch
            )
        )
    return result


def match_observations(conn, rows):
    known_ids = existing_page_ids(
        conn, [page_id for page_id, _, _ in rows if page_id is not None]
    )
    title_ids = lookup_page_ids(
        conn, [title for page_id, title, _ in rows if page_id is None]
    )
    counts = {}
    unmatched = 0
    for page_id, title, views in rows:
        matched_id = page_id if page_id in known_ids else (
            title_ids.get(normalize_title(title)) if page_id is None else None
        )
        if matched_id is None:
            unmatched += 1
            continue
        counts[matched_id] = max(counts.get(matched_id, views), views)
    return sorted(counts.items()), unmatched


def add_observations(month_stats, day, observations):
    for page_id, views in observations:
        item = month_stats.setdefault(page_id, {
            "days_observed": 0,
            "dp_views_sum": 0,
            "first_seen": day,
            "last_seen": day,
        })
        item["days_observed"] += 1
        item["dp_views_sum"] += views
        item["first_seen"] = min(item["first_seen"], day)
        item["last_seen"] = max(item["last_seen"], day)


def month_groups(start_date, end_date):
    groups = {}
    for offset in range((end_date - start_date).days + 1):
        day = start_date + timedelta(days=offset)
        groups.setdefault(day.strftime("%Y-%m"), []).append(day)
    return groups


def ensure_stage(conn):
    conn.executescript(f"""
        CREATE TABLE IF NOT EXISTS {STAGE} (
            page_id INTEGER PRIMARY KEY,
            days_observed INTEGER NOT NULL,
            dp_views_sum INTEGER NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {STAGE_MONTHS} (
            month TEXT PRIMARY KEY,
            missing_days INTEGER NOT NULL,
            unmatched INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS {STAGE_WINDOW} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL
        );
    """)
    conn.commit()


def stage_window(conn):
    row = conn.execute(
        f"SELECT window_start, window_end FROM {STAGE_WINDOW} WHERE id = 1"
    ).fetchone()
    return tuple(row) if row else None


def initialize_stage(conn, start_date, end_date):
    with conn:
        if stage_window(conn) != (start_date, end_date):
            conn.execute(f"DELETE FROM {STAGE}")
            conn.execute(f"DELETE FROM {STAGE_MONTHS}")
            conn.execute(f"DELETE FROM {STAGE_WINDOW}")
            conn.execute(
                f"INSERT INTO {STAGE_WINDOW} (id, window_start, window_end) VALUES (1, ?, ?)",
                (start_date, end_date),
            )


def completed_months(conn):
    return {row[0] for row in conn.execute(f"SELECT month FROM {STAGE_MONTHS}")}


def store_month(conn, month, stats, missing_days, unmatched):
    if conn.execute(
        f"SELECT 1 FROM {STAGE_MONTHS} WHERE month = ?", (month,)
    ).fetchone():
        return False
    with conn:
        conn.executemany(
            f"""INSERT INTO {STAGE} (
                page_id, days_observed, dp_views_sum, first_seen, last_seen
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(page_id) DO UPDATE SET
                days_observed = days_observed + excluded.days_observed,
                dp_views_sum = dp_views_sum + excluded.dp_views_sum,
                first_seen = min(first_seen, excluded.first_seen),
                last_seen = max(last_seen, excluded.last_seen)""",
            [
                (page_id, row["days_observed"], row["dp_views_sum"],
                 row["first_seen"], row["last_seen"])
                for page_id, row in stats.items()
            ],
        )
        conn.execute(
            f"INSERT INTO {STAGE_MONTHS} (month, missing_days, unmatched) VALUES (?, ?, ?)",
            (month, missing_days, unmatched),
        )
    return True


def finalize_summary(conn):
    window = stage_window(conn)
    if window is None:
        raise RuntimeError("No ten-year import is staged")
    start_date, end_date = window
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {SUMMARY} (
        page_id INTEGER PRIMARY KEY REFERENCES articles(page_id),
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        days_observed INTEGER NOT NULL,
        dp_views_sum INTEGER NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL
    )""")
    with conn:
        conn.execute(f"DELETE FROM {SUMMARY}")
        conn.execute(
            f"""INSERT INTO {SUMMARY} (
                page_id, window_start, window_end, days_observed,
                dp_views_sum, first_seen, last_seen
            ) SELECT page_id, ?, ?, days_observed, dp_views_sum, first_seen, last_seen
              FROM {STAGE}""",
            (start_date, end_date),
        )
        page_count = conn.execute(f"SELECT count(*) FROM {SUMMARY}").fetchone()[0]
        missing_days, unmatched = conn.execute(
            f"SELECT sum(missing_days), sum(unmatched) FROM {STAGE_MONTHS}"
        ).fetchone()
        conn.execute(f"DROP TABLE {STAGE}")
        conn.execute(f"DROP TABLE {STAGE_MONTHS}")
        conn.execute(f"DROP TABLE {STAGE_WINDOW}")
    return page_count, missing_days or 0, unmatched or 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/wikipedia.sqlite"))
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error(f"SQLite database not found: {args.db}")

    with sqlite3.connect(args.db) as conn:
        ensure_stage(conn)
        window = stage_window(conn)
        latest_rows = None
        if window:
            start_date, end_date = (date.fromisoformat(value) for value in window)
        else:
            end_date, latest_rows = latest_available_day()
            start_date, end_date = date_window(end_date, WINDOW_DAYS)
            initialize_stage(conn, start_date.isoformat(), end_date.isoformat())

        groups = month_groups(start_date, end_date)
        done = completed_months(conn)
        print(
            f"Loading U.S. en.wikipedia DP observations from {start_date} through {end_date}; "
            f"{len(done):,}/{len(groups):,} months already checkpointed…",
            flush=True,
        )
        for index, (month, days) in enumerate(groups.items(), 1):
            if month in done:
                continue
            month_stats = {}
            missing_days = unmatched = 0
            for day in days:
                rows = latest_rows if day == end_date and latest_rows is not None else fetch_day_rows(day)
                if rows:
                    observations, day_unmatched = match_observations(conn, rows)
                    add_observations(month_stats, day.isoformat(), observations)
                    unmatched += day_unmatched
                else:
                    missing_days += 1
            store_month(conn, month, month_stats, missing_days, unmatched)
            done.add(month)
            print(
                f"Processed {month} ({index}/{len(groups)}); "
                f"{len(month_stats):,} matched pages this month.",
                flush=True,
            )

        page_count, missing_days, unmatched = finalize_summary(conn)
        print(
            f"Stored {page_count:,} pages in {SUMMARY}; {missing_days} days had no U.S. "
            f"English-Wikipedia rows; {unmatched:,} rows did not match articles."
        )


if __name__ == "__main__":
    main()
