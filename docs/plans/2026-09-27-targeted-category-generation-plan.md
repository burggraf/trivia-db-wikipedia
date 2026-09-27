# Targeted Category Generation Implementation Plan

> **REQUIRED SUB-SKILL:** Use the executing-plans skill to implement this plan task-by-task.

**Goal:** Add an opt-in targeted generator that creates 25 new drafts each for Food & Drink, Games & Hobbies, Arts & Design, and Religion & Philosophy without changing standard popularity-ordered generation.

**Architecture:** Create `generate_targeted_trivia.py`, a separate CLI using the existing question contract, Pi invocation, validation, and atomic spool writer. It processes one target category at a time: cheap title/description keyword hints select plausible unprocessed candidates, Luna is instructed to return a question in that exact category or a skip, and a single parent importer writes SQLite. Targeted skips/failures live in separate tables and do not create or update `article_jobs`, preserving pages for normal generation.

**Tech Stack:** Python standard library, SQLite, existing `generate_trivia.py` helpers, Pi CLI / `openai-codex/gpt-6-luna` with minimal thinking.

---

### Task 1: Add category-aware prompt and validation

**Files:**
- Modify: `generate_trivia.py`
- Modify: `test_generate_trivia.py`

**Step 1: Write failing tests**

Add a test that calls `make_prompt(article, categories=('food_drink',))` and asserts only that category is supplied. Add a test that a valid Food & Drink response is rejected when `validate(..., categories=('games_hobbies',))` is used.

**Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest test_generate_trivia.PipelineTests.test_targeted_category_validation -v`

Expected: FAIL because the helpers do not accept category constraints.

**Step 3: Implement the smallest compatible helpers**

Add optional `categories` arguments to `make_prompt`, `validate`, and `pi_generate`. Default to all existing categories, preserving standard generation. Reject unknown requested category IDs before model dispatch.

**Step 4: Run focused tests**

Run: `python3 -m unittest test_generate_trivia -v`

Expected: PASS.

### Task 2: Implement targeted candidate routing and durable importer

**Files:**
- Create: `generate_targeted_trivia.py`
- Create: `test_generate_targeted_trivia.py`

**Step 1: Write failing tests**

Test that keyword hints select a plausible Food & Drink page and do not select an unrelated page; test that a no-match targeted artifact records `no_match` but does not create an `article_jobs` row; test that a valid targeted result creates exactly one normal `article_jobs`/`questions` pair; test that spool re-import is idempotent.

**Step 2: Run focused tests to verify they fail**

Run: `python3 -m unittest test_generate_targeted_trivia -v`

Expected: FAIL because the targeted module does not exist.

**Step 3: Implement the smallest targeted mode**

Use four intentionally small keyword sets in title/description text:

```python
CATEGORY_HINTS = {
  'food_drink': ('food', 'restaurant', 'beer', 'wine', 'coffee', 'tea', 'pizza', 'cuisine', ...),
  'games_hobbies': ('game', 'chess', 'poker', 'board game', 'video game', 'toy', ...),
  'arts_design': ('art', 'artist', 'painting', 'museum', 'architecture', 'sculpture', ...),
  'religion_philosophy': ('religion', 'philosophy', 'church', 'buddh', 'islam', 'christian', ...),
}
```

Add `targeted_attempts` and `targeted_spool_imports` tables. Spool artifacts include the complete source snapshot and target category. The importer:

- validates model output against only the target category;
- inserts `article_jobs` and a draft question only for valid results;
- records `done`, `no_match`, `failed`, or `superseded` targeted attempts;
- records an artifact ID in the import table within the same transaction; and
- deletes the artifact only after commit.

Skip candidates that have an existing `article_jobs` row or a terminal same-category targeted attempt. Process target categories sequentially; use `ThreadPoolExecutor` only for concurrent Pi workers per category. The database lock path must match standard generation.

**Step 4: Run focused tests**

Run: `python3 -m unittest test_generate_targeted_trivia -v`

Expected: PASS.

### Task 3: Add CLI/docs and run the 100-question pilot

**Files:**
- Modify: `README.md`
- Modify: `test_generate_targeted_trivia.py`

**Step 1: Add CLI coverage**

Test parsing category CSV and reject unknown/unsupported target category IDs and non-positive per-category counts.

**Step 2: Document usage**

Document that this is opt-in and does not alter standard generation; describe heuristic prefiltering, exact model/validator category enforcement, and separate miss tracking.

```sh
python3 generate_targeted_trivia.py \
  --categories food_drink,games_hobbies,arts_design,religion_philosophy \
  --per-category 25 --workers 8
```

**Step 3: Verify**

Run:

```sh
python3 -m unittest -q
python3 -m py_compile generate_trivia.py generate_targeted_trivia.py
python3 generate_targeted_trivia.py --categories food_drink,games_hobbies,arts_design,religion_philosophy --per-category 25 --workers 8
```

Confirm exactly 25 targeted `done` attempts per category, exactly 100 new `questions`, no duplicate page IDs, no spool artifacts, and that targeted misses created no normal `article_jobs` rows.
