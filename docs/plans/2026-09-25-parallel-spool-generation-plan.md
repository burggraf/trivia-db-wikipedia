# Parallel Spool Generation Implementation Plan

> **REQUIRED SUB-SKILL:** Use the executing-plans skill to implement this plan task-by-task.

**Goal:** Generate draft trivia questions concurrently while keeping one SQLite importer and preserving restart safety.

**Architecture:** `generate_trivia.py` will claim jobs in SQLite before dispatching them, run a bounded number of worker subprocesses, and accept each worker's atomically written JSON result through a spool directory. The parent alone validates and writes attempts/questions in one SQLite transaction, deleting a result only after that commit. A later invocation ingests durable spool files before choosing new work, so a parent crash cannot lose a completed worker result.

**Tech Stack:** Python standard library (`concurrent.futures`, `json`, `os.replace`, `pathlib`, SQLite) and Pi CLI with `openai-codex/gpt-6-luna` at minimal reasoning.

---

### Task 1: Define atomic spool artifacts

**Files:**
- Modify: `generate_trivia.py`
- Modify: `test_generate_trivia.py`

**Step 1: Write failing tests**

Add a test that constructs a successful worker result, writes it with `write_spool_result()`, and asserts that the final result file is valid JSON with `page_id`, `source_revision_id`, `prompt`, `raw_response`, and `usage`; assert no `.tmp` artifact remains. Add a test that a spool result is inserted exactly once and deleted only after the importer commits.

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_generate_trivia.PipelineTests.test_spool_result_is_atomic -v`

Expected: FAIL because spool helpers do not exist.

**Step 3: Implement minimal artifact helpers**

Add `spool_path()`, `write_spool_result()`, and `ingest_spool()`:

```python
def write_spool_result(spool_dir, result):
    final = spool_dir / f'{result["page_id"]}-{result["attempt_id"]}.json'
    temp = final.with_suffix('.json.tmp')
    temp.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, final)
    return final
```

The importer verifies that the stored job revision equals the result revision, calls existing `validate()`, records the immutable attempt, and inserts exactly one question. It only unlinks the result after `with conn:` succeeds. It treats malformed/incompatible results as failed attempts rather than silently accepting them.

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_generate_trivia -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add generate_trivia.py test_generate_trivia.py
git commit -m "feat: add durable trivia generation spool"
```

### Task 2: Add bounded workers and recovery

**Files:**
- Modify: `generate_trivia.py`
- Modify: `test_generate_trivia.py`

**Step 1: Write failing tests**

Add a test injecting a deterministic worker function and `workers=2`; it must observe two jobs active before the importer runs, then prove exactly one `questions` and `generation_attempts` record per article. Add a recovery test: place a valid spool file before `run_batch()` and assert it imports without another model call.

**Step 2: Run test to verify it fails**

Run: `python3 -m unittest test_generate_trivia.PipelineTests.test_ingests_spool_before_dispatch -v`

Expected: FAIL because `workers` and pre-dispatch recovery do not exist.

**Step 3: Implement bounded dispatch**

Use `ThreadPoolExecutor(max_workers=workers)` only to manage external worker processes. A worker runs the existing Pi call and writes an artifact; it never opens the database. The parent claims no more than `workers` runnable articles, then repeats: ingest finished spool results, dispatch further work, and wait for a completed worker. It writes claim state before dispatch, handles `running` state on restart, and keeps failures eligible only through `--retry-failed`.

Add CLI options:

```text
--workers N             concurrent Pi workers; default 8
--spool-dir PATH        durable result queue; default <db>.spool
```

Reject non-positive worker counts. The default remains bounded to avoid provider rate limiting; there is no "unlimited" mode.

**Step 4: Run test to verify it passes**

Run: `python3 -m unittest test_generate_trivia -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add generate_trivia.py test_generate_trivia.py
git commit -m "feat: run trivia generation with bounded workers"
```

### Task 3: Document and benchmark safely

**Files:**
- Modify: `README.md`
- Modify: `test_generate_trivia.py` (only if CLI behavior needs a regression test)

**Step 1: Update docs**

Document `--workers 8`, `--spool-dir`, atomic recovery behavior, and that higher worker counts can produce rate-limit failures. State that workers use GPT-6 Luna minimal exactly as before and the importer remains the sole SQLite writer.

**Step 2: Verify behavior**

Run:

```bash
python3 -m unittest -v
python3 -m py_compile generate_trivia.py
python3 generate_trivia.py --all --limit 16 --workers 8
```

Expected: all tests pass; the bounded live run records at most 16 new attempts, leaves no completed spool files, and reports wall-clock duration plus job counts. Do not use existing completed pilot articles for the timing comparison.

**Step 3: Inspect evidence**

Run:

```bash
sqlite3 data/trivia.sqlite 'SELECT status, COUNT(*) FROM article_jobs GROUP BY status;'
find data -maxdepth 1 -type d -name 'trivia.sqlite.spool' -print
```

Confirm a single importer has recorded each attempt and no result artifact remains after a successful import.

**Step 4: Commit**

```bash
git add README.md generate_trivia.py test_generate_trivia.py
git commit -m "docs: document parallel trivia generation"
```
