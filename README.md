# Local Wikipedia abstract database

Imports the public English Structured Contents Parquet release from Hugging Face into SQLite. It keeps abstracts at least 160 characters long and excludes first-line `may refer to:` / `may also refer to:` disambiguation leads and titles marked `(disambiguation)`. The script uses the DuckDB CLI and Python's standard library; it does not need the DuckDB Python package.

## Run

```sh
python3 -m unittest -v
python3 build_wikipedia.py                 # first 3,000 non-empty abstracts
python3 build_wikipedia.py --all           # continue into the full dataset
```

The database is `data/wikipedia.sqlite`. Existing rows that fail these filters are pruned when the importer is run. The full import can take a while and transfer several GB of selected Parquet columns. The source is pinned to Hugging Face revision `417c267bb457fa645c22eb3b5c77764963194c70` (snapshot dated 2026-05-13). Re-running imports is safe: page IDs are primary keys, and duplicate pages keep the highest revision ID.

Override the DuckDB executable or database path if needed:

```sh
python3 build_wikipedia.py --duckdb /opt/homebrew/bin/duckdb --db data/wikipedia.sqlite --limit 5000
```

Useful local check:

```sh
sqlite3 data/wikipedia.sqlite 'SELECT page_id, title, wikidata_qid, abstract FROM articles LIMIT 5;'
```

## Trivia article shortlist

Create a deterministic, editable candidate manifest from the top 10,000 articles by ten-year U.S. popularity:

```sh
python3 select_trivia_articles.py
```

This opens `data/wikipedia.sqlite` read-only and writes `data/trivia_candidates.csv`. The report includes rank, popularity, article/source metadata, abstract word count, review flags, and an initial question-count ceiling. The ceiling is not a quota; shorter abstracts are flagged for review. Article-level category guesses are intentionally omitted because a single article can support questions in different categories.

To create a smaller test manifest or choose a different output path:

```sh
python3 select_trivia_articles.py --limit 100 --output /tmp/trivia-candidates.csv
```

Edit `include_in_pilot` to `1` for manually selected pilot articles and use `review_notes` for selection context. The `question_count_override` column is informational only: the current generator creates at most one question per `page_id`. Existing manifests are never overwritten by default. Use `--force` only when you intend to replace the output.

## Resumable trivia question generation

The generator uses the edited candidate manifest as its source snapshot. It does **not** write to `data/wikipedia.sqlite`. It creates a separate `data/trivia.sqlite` containing article jobs, validated draft questions, and every completed generation attempt (prompt, raw reply, status, token counts, API-equivalent cost). Prior hand-written pilot JSONL drafts are **not** automatically imported or counted as completed jobs.

```sh
python3 generate_trivia.py                                  # attempt one selected pilot article
python3 generate_trivia.py --limit 20                       # next 20, with 8 workers by default
python3 generate_trivia.py --all --limit 100 --workers 8     # continue through all 10,000 ranked candidates
python3 generate_trivia.py --all --retry-failed --limit 10
```

Each worker starts a fresh authenticated Pi run using `openai-codex/gpt-6-luna` with `minimal` reasoning, no tools or session. The default is eight concurrent workers; use `--workers N` to lower or raise that bounded limit. Higher values can trigger provider rate limits and do not necessarily improve throughput. Workers atomically write results to `data/trivia.sqlite.spool` (override with `--spool-dir`); the parent is the only SQLite writer, imports these files before new work on restart, and deletes each only after its transaction commits. Run `pi auth check --provider openai-codex --model gpt-6-luna` first if needed. Accepted items are **drafts**, not publication-ready questions: source evidence, answer-in-stem, schema, and some orphaned-reference patterns are checked automatically, but factual support, distractors, standalone clarity, sensitivity, and fun facts still need human review. A model may return a skip reason instead of a weak question. Failed attempts are held for `--retry-failed`; interrupted `running` jobs are automatically retried on restart. Repeated commands skip completed and skipped articles, preserving stable question IDs. Each invocation attempts at most `--limit` articles (default 1). Avoid changing the manifest source snapshot between runs; existing jobs keep their original source text and revision.

```sh
sqlite3 data/trivia.sqlite 'SELECT status, COUNT(*) FROM article_jobs GROUP BY status;'
sqlite3 data/trivia.sqlite 'SELECT page_id, question, a FROM questions LIMIT 5;'
```

## U.S. popularity

```sh
python3 import_us_pageviews.py              # latest available 365-day window
python3 import_us_pageviews.py --days 1826  # five-year window; keeps the 365-day table
python3 import_us_pageviews_10y.py          # ten-year DP dataset; keeps both API tables
```

The API script creates `us_page_popularity_365d` and `us_page_popularity_5y`. The ten-year script creates `us_page_popularity_10y`. All join to `articles` by `page_id`.

```sql
SELECT a.title, p.days_in_top_1000, p.observed_views_ceil_sum, p.best_rank
FROM us_page_popularity_5y AS p
JOIN articles AS a USING (page_id)
ORDER BY p.observed_views_ceil_sum DESC
LIMIT 25;
```

The free [Wikimedia country top-pages API](https://wikimedia.org/api/rest_v1/metrics/pageviews/api-spec.json) publishes at most 1,000 pages per country per day across all Wikimedia projects; its daily country lists start on 2021-01-01 ([Wikimedia Analytics discussion](https://lists.wikimedia.org/hyperkitty/list/analytics@lists.wikimedia.org/message/STLYZXCF442KJZ6457TMK5XUMJNTA6PQ/)). The API importer keeps matching `en.wikipedia` pages. `views_ceil` is privacy-rounded, and its sum is only for days a page appears in the published top list—not its exact window total.

For ten years, `import_us_pageviews_10y.py` uses Wikimedia's [differentially private country/project/page datasets](https://analytics.wikimedia.org/published/datasets/country_project_page/00_README.html), with historical data from [2017–2023](https://analytics.wikimedia.org/published/datasets/country_project_page_historical/00_README.html) and [2015–2017](https://analytics.wikimedia.org/published/datasets/country_project_page_historical_pre_2017/00_README.html). Its `dp_views_sum` is an approximate sum of released noisy counts; low-volume pages are omitted, and release methods changed across periods. U.S. rows are missing for part of 2023, and pre-2017 rows lack page IDs, so those titles are matched by name. This is a popularity signal, not a complete or exact ten-year total.

To query the ten-year aggregate:

```sql
SELECT a.title, p.days_observed, p.dp_views_sum, p.first_seen, p.last_seen
FROM us_page_popularity_10y AS p
JOIN articles AS a USING (page_id)
ORDER BY p.dp_views_sum DESC
LIMIT 25;
```

The ten-year import downloads each full daily TSV (sampled files ranged from about 3 MB to 20 MB), so expect a large transfer and long run. It checkpoints each month in the local SQLite file so it can resume after interruption. Requests are sequential and identify this project with a User-Agent.

## Hugging Face account

No account is required for this public dataset. If DuckDB reports HTTP 429, wait a few minutes and retry, or create a free account at [huggingface.co/join](https://huggingface.co/join). Create a read token at [Settings → Access Tokens](https://huggingface.co/settings/tokens), then set `HF_TOKEN` in your terminal before running the script. The script passes it to DuckDB without saving it in this project. Never commit the token.

DuckDB may download its `httpfs` extension into its normal user-level extension cache the first time it runs; no Python package or virtual environment is installed by this project.

The article rows retain their source URL, revision ID, and license metadata for provenance and attribution (CC BY-SA 4.0 in the published dataset).
