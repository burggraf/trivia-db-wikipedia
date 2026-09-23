#!/usr/bin/env python3
"""Import English Wikipedia abstracts from Wikimedia's hosted Parquet files."""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


SOURCE_REVISION = "417c267bb457fa645c22eb3b5c77764963194c70"
MIN_ABSTRACT_CHARS = 160
DISAMBIGUATION_PHRASES = ("may refer to:", "may also refer to:")
SOURCE_PATH = (
    "hf://datasets/wikimedia/structured-wikipedia@"
    f"{SOURCE_REVISION}/enwiki/data/*.parquet"
)
COLUMNS = (
    "page_id", "title", "wikidata_qid", "description", "abstract", "url",
    "revision_id", "modified_at", "license_json",
)
SOURCE_SQL = f"""
SELECT
    identifier AS page_id,
    name AS title,
    main_entity.identifier AS wikidata_qid,
    description,
    abstract,
    url,
    version.identifier AS revision_id,
    CAST(date_modified AS VARCHAR) AS modified_at,
    to_json(license) AS license_json
FROM read_parquet('{SOURCE_PATH}')
WHERE abstract IS NOT NULL
  AND length(trim(abstract)) >= {MIN_ABSTRACT_CHARS}
  AND lower(split_part(abstract, chr(10), 1)) NOT LIKE '%may refer to:%'
  AND lower(split_part(abstract, chr(10), 1)) NOT LIKE '%may also refer to:%'
  AND lower(name) NOT LIKE '%(disambiguation)%'
"""
INSERT_SQL = """
INSERT INTO articles (
    page_id, title, wikidata_qid, description, abstract, url,
    revision_id, modified_at, license_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(page_id) DO UPDATE SET
    title = excluded.title,
    wikidata_qid = excluded.wikidata_qid,
    description = excluded.description,
    abstract = excluded.abstract,
    url = excluded.url,
    revision_id = excluded.revision_id,
    modified_at = excluded.modified_at,
    license_json = excluded.license_json
WHERE COALESCE(excluded.revision_id, -1) > COALESCE(articles.revision_id, -1)
"""


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
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
        CREATE TABLE IF NOT EXISTS dataset_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.executemany(
        "INSERT OR REPLACE INTO dataset_metadata (key, value) VALUES (?, ?)",
        [
            ("source", "wikimedia/structured-wikipedia"),
            ("source_revision", SOURCE_REVISION),
            ("snapshot_date", "2026-05-13"),
        ],
    )
    conn.commit()


def row_from_json(line):
    record = json.loads(line)
    license_json = record["license_json"]
    if license_json is not None and not isinstance(license_json, str):
        record["license_json"] = json.dumps(
            license_json, ensure_ascii=False, separators=(",", ":")
        )
    return tuple(record[column] for column in COLUMNS)


def is_trivia_candidate(title, abstract):
    if abstract is None:
        return False
    abstract = abstract.strip()
    first_line = abstract.splitlines()[0].lower() if abstract else ""
    return (
        len(abstract) >= MIN_ABSTRACT_CHARS
        and not any(phrase in first_line for phrase in DISAMBIGUATION_PHRASES)
        and "(disambiguation)" not in (title or "").lower()
    )


def prune_existing_rows(conn):
    first_line = """CASE WHEN instr(abstract, char(10)) > 0
        THEN substr(abstract, 1, instr(abstract, char(10)) - 1)
        ELSE abstract END"""
    with conn:
        cursor = conn.execute(
            f"""DELETE FROM articles
                WHERE length(trim(abstract)) < ?
                   OR lower({first_line}) LIKE '%may refer to:%'
                   OR lower({first_line}) LIKE '%may also refer to:%'
                   OR lower(title) LIKE '%(disambiguation)%'""",
            (MIN_ABSTRACT_CHARS,),
        )
    return cursor.rowcount


def import_rows(conn, rows, batch_size=1000):
    """Insert eligible DuckDB rows in small committed batches; return rows read."""
    batch = []
    imported = 0
    for row in rows:
        if not is_trivia_candidate(row[1], row[4]):
            continue
        batch.append(row)
        if len(batch) >= batch_size:
            with conn:
                conn.executemany(INSERT_SQL, batch)
            imported += len(batch)
            batch.clear()
    if batch:
        with conn:
            conn.executemany(INSERT_SQL, batch)
        imported += len(batch)
    return imported


def json_row_batches(stream, batch_size=1000):
    rows = []
    for line in stream:
        rows.append(row_from_json(line))
        if len(rows) >= batch_size:
            yield rows
            rows = []
    if rows:
        yield rows


def positive_int(value):
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/wikipedia.sqlite"))
    parser.add_argument(
        "--duckdb", default=shutil.which("duckdb") or "/opt/homebrew/bin/duckdb",
        help="DuckDB CLI path (default: found on PATH or /opt/homebrew/bin/duckdb)",
    )
    parser.add_argument(
        "--limit", type=positive_int, default=3000,
        help="sample size (default: 3000); use --all for the complete dataset",
    )
    parser.add_argument("--all", action="store_true", help="import all eligible articles")
    args = parser.parse_args()
    limit = None if args.all else args.limit

    if not Path(args.duckdb).is_file():
        parser.error(f"DuckDB CLI not found: {args.duckdb}")

    sql = "INSTALL httpfs; LOAD httpfs;"
    token = os.environ.get("HF_TOKEN")
    if token:
        sql += " CREATE SECRET hf_token (TYPE huggingface, TOKEN "
        sql += "'" + token.replace("'", "''") + "');"
    sql += SOURCE_SQL
    if limit is not None:
        sql += f" LIMIT {limit}"

    args.db.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db)
    ensure_schema(db)
    removed = prune_existing_rows(db)
    if removed:
        print(f"Removed {removed:,} existing rows excluded by the filters.", flush=True)
    command = [args.duckdb, "-jsonlines", "-noheader", "-no-init", "-bail"]

    try:
        remote = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8",
        )
        remote.stdin.write(sql)
        remote.stdin.close()
        imported = 0
        try:
            for rows in json_row_batches(remote.stdout):
                imported += import_rows(db, rows)
                if imported and imported % 10000 == 0:
                    print(f"Imported {imported:,} rows…", flush=True)
            status = remote.wait()
        except BaseException:
            remote.terminate()
            remote.wait()
            raise
        if status:
            raise RuntimeError(
                f"DuckDB exited with status {status}; if the error is HTTP 429, "
                "wait a few minutes or configure HF_TOKEN as described in README.md"
            )

        db.executescript("""
            CREATE INDEX IF NOT EXISTS articles_title_idx ON articles(title);
            CREATE INDEX IF NOT EXISTS articles_qid_idx ON articles(wikidata_qid);
        """)
        db.commit()
    except Exception as exc:
        if "429" in str(exc) or "Too Many Requests" in str(exc):
            print(
                "Hugging Face throttled the download. Wait a few minutes and retry; "
                "a free account token can raise the request limit (see README.md).",
                file=sys.stderr,
            )
        raise
    finally:
        db.close()

    with sqlite3.connect(args.db) as check:
        total = check.execute("SELECT count(*) FROM articles").fetchone()[0]
    print(f"Imported {imported:,} eligible rows into {args.db} ({total:,} total).")


if __name__ == "__main__":
    main()
