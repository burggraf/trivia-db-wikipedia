#!/usr/bin/env python3
"""Refresh a U.S. Wikipedia popularity summary from Wikimedia's API."""

import argparse
import json
import sqlite3
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://wikimedia.org/api/rest_v1/metrics/pageviews/top-per-country/US/all-access/{date}"
USER_AGENT = "trivia-db-wikipedia/0.1 (https://github.com/burggraf/trivia-db-wikipedia)"
TABLES = {
    365: "us_page_popularity_365d",
    1826: "us_page_popularity_5y",
}


def summary_table(days):
    try:
        return TABLES[days]
    except KeyError as exc:
        raise ValueError("--days must be 365 or 1826") from exc


def date_window(end_date, days=365):
    if days < 1:
        raise ValueError("days must be positive")
    return end_date - timedelta(days=days - 1), end_date


def normalize_title(title):
    return title.replace("_", " ").casefold()


def lookup_page_ids(conn, titles):
    titles_by_key = {normalize_title(title): title.replace("_", " ") for title in titles}
    if not titles_by_key:
        return {}
    placeholders = ",".join("?" for _ in titles_by_key)
    rows = conn.execute(
        f"""SELECT page_id, title FROM articles
            WHERE title IN ({placeholders})
            ORDER BY COALESCE(revision_id, -1) DESC, modified_at DESC, page_id DESC""",
        tuple(titles_by_key.values()),
    )
    page_ids = {}
    for page_id, title in rows:
        page_ids.setdefault(normalize_title(title), page_id)
    return page_ids


def collect_day(conn, day, payload, stats):
    articles = [
        article
        for item in payload.get("items", [])
        if item.get("country") == "US"
        for article in item.get("articles", [])
        if article.get("project") == "en.wikipedia"
    ]
    page_ids = lookup_page_ids(conn, [article["article"] for article in articles])
    matched = unmatched = 0
    seen = set()
    for article in articles:
        page_id = page_ids.get(normalize_title(article["article"]))
        if page_id is None:
            unmatched += 1
            continue
        if page_id in seen:
            continue
        seen.add(page_id)
        views = int(article["views_ceil"])
        rank = int(article["rank"])
        item = stats.setdefault(page_id, {
            "days_in_top_1000": 0,
            "observed_views_ceil_sum": 0,
            "best_rank": rank,
            "last_seen": day,
        })
        item["days_in_top_1000"] += 1
        item["observed_views_ceil_sum"] += views
        item["best_rank"] = min(item["best_rank"], rank)
        item["last_seen"] = max(item["last_seen"], day)
        matched += 1
    return matched, unmatched


def write_summary(conn, stats, start_date, end_date, table=TABLES[365]):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
        page_id INTEGER PRIMARY KEY REFERENCES articles(page_id),
        window_start TEXT NOT NULL,
        window_end TEXT NOT NULL,
        days_in_top_1000 INTEGER NOT NULL,
        observed_views_ceil_sum INTEGER NOT NULL,
        best_rank INTEGER NOT NULL,
        last_seen TEXT NOT NULL
    )""")
    with conn:
        conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            f"""INSERT INTO {table} (
                page_id, window_start, window_end, days_in_top_1000,
                observed_views_ceil_sum, best_rank, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    page_id, start_date, end_date, row["days_in_top_1000"],
                    row["observed_views_ceil_sum"], row["best_rank"], row["last_seen"],
                )
                for page_id, row in stats.items()
            ],
        )
    return len(stats)


def fetch_day(day):
    url = API_URL.format(date=day.strftime("%Y/%m/%d"))
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
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
        except (URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return None


def latest_available_day(today=None, lookback=7):
    today = today or date.today()
    for days_ago in range(lookback + 1):
        day = today - timedelta(days=days_ago)
        payload = fetch_day(day)
        if payload is not None:
            return day, payload
    raise RuntimeError(f"No U.S. pageview data found in the last {lookback + 1} days")


def positive_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/wikipedia.sqlite"))
    parser.add_argument(
        "--days", type=positive_int, default=365,
        help="window length: 365 (1 year) or 1826 (5 years)",
    )
    args = parser.parse_args()
    try:
        table = summary_table(args.days)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.db.is_file():
        parser.error(f"SQLite database not found: {args.db}")

    end_date, latest_payload = latest_available_day()
    start_date, end_date = date_window(end_date, args.days)
    print(f"Fetching U.S. top-page observations from {start_date} through {end_date}…", flush=True)

    stats = {}
    matched = unmatched = missing_days = 0
    with sqlite3.connect(args.db) as conn:
        for index in range(args.days):
            day = start_date + timedelta(days=index)
            payload = latest_payload if day == end_date else fetch_day(day)
            if payload is None:
                missing_days += 1
            else:
                day_matched, day_unmatched = collect_day(conn, day.isoformat(), payload, stats)
                matched += day_matched
                unmatched += day_unmatched
            if (index + 1) % 30 == 0 or index + 1 == args.days:
                print(
                    f"Processed {index + 1}/{args.days} days; "
                    f"{len(stats):,} matched pages so far.",
                    flush=True,
                )

        count = write_summary(
            conn, stats, start_date.isoformat(), end_date.isoformat(), table
        )

    print(
        f"Stored {count:,} pages in {table}; {matched:,} matched page-days, "
        f"{unmatched:,} unmatched top-list entries, {missing_days} unavailable days."
    )


if __name__ == "__main__":
    main()
