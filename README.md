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

## Hugging Face account

No account is required for this public dataset. If DuckDB reports HTTP 429, wait a few minutes and retry, or create a free account at [huggingface.co/join](https://huggingface.co/join). Create a read token at [Settings → Access Tokens](https://huggingface.co/settings/tokens), then set `HF_TOKEN` in your terminal before running the script. The script passes it to DuckDB without saving it in this project. Never commit the token.

DuckDB may download its `httpfs` extension into its normal user-level extension cache the first time it runs; no Python package or virtual environment is installed by this project.

The article rows retain their source URL, revision ID, and license metadata for provenance and attribution (CC BY-SA 4.0 in the published dataset).
