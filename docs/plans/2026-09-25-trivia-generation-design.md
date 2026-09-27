# Wikipedia-to-trivia pipeline: initial plan

Date: 2026-09-25
Status: The read-only candidate selector and resumable single-question-per-page generator are implemented. The local trivia database contains 814 draft questions from 814 distinct pages, generated with GPT-6 Luna minimal reasoning; none are production-approved. Generation attempts and API-equivalent costs are recorded under gitignored `data/`.

## Goal and agreed scope

Generate multiple-choice questions for average US adults in a pub-trivia setting. Favor balanced, mostly evergreen topics; flag sensitive and time-sensitive material for review. Current policy: generate at most one question per `page_id`; multi-question-per-page generation is deferred.

Every question must have exactly one fixed top-level `category`, one nonempty free-text `subcategory`, and an integer difficulty `level` from 1 through 9. These are required on drafts as well as approved questions, not optional suggestions.

**Popularity chooses promising subjects; it does not establish that a particular fact makes a good question.** Recognizable topics can still produce obscure, dull, or ambiguous trivia.

## 1. What we have

Read-only inspection of `data/wikipedia.sqlite` found:

- 5,133,588 articles; 1,042,639 rows in `us_page_popularity_10y`.
- Ten-year popularity window: 2016-09-23 through 2026-09-22.
- Article snapshot metadata: 2026-05-13. Popularity being newer does not make article facts current.
- Article IDs, titles, descriptions, abstracts, Wikidata IDs, URLs, revision IDs, modification dates, and license metadata already exist.
- `dp_views_sum` provides a popularity ordering; `days_observed` provides a rough persistence signal. There is no stored rank column.
- The existing 365-day and five-year tables offer optional additional signals, but use a different collection method. Their counts should not be added to the ten-year totals.

Some surprisingly specialist pages rank very highly: JSON-LD, RDFa, and Microdata (HTML) rank 10–12 by ten-year views. Their causes have not been investigated. This is a reason to sanity-check rankings and assess suitability, not evidence that those topics are familiar to the average player.

The actual abstracts are useful but bounded: the median is 286 words for ranks 1,001–5,000. The United States abstract has 563 words and World War II has 662. Neither gives us the full article.

## 2. Recommended approach

Three reasonable starting points:

1. **Raw top-N articles:** cheapest, but specialist topics and traffic-heavy categories can dominate.
2. **Popularity shortlist + suitability check + balanced batches:** recommended; understandable, adjustable, and inexpensive to pilot.
3. **LLM-score every article before generation:** potentially flexible, but unnecessary expense and complexity before we know what works.

Start with option 2. Use Python scripts and SQLite, matching the existing project. Do not introduce a workflow service, vector database, or model orchestration framework.

## 3. Select articles

### Build a reproducible shortlist

Start with the top **10,000 articles by `dp_views_sum`**, breaking ties by `page_id`. This is an initial pool, not a claim that everything below rank 10,000 is obscure or that all 10,000 should be processed immediately.

Save the selection policy version, popularity window, rank, raw popularity values, abstract word count, and selection/skip reason with each selected article. A rerun should not silently rearrange an existing batch.

Use `days_observed` as a soft supporting signal, not a hard age requirement: newer well-known subjects should remain eligible. Missing/noisy observations mean it is not an exact readership measure. The aggregate alone cannot establish whether interest was steady or driven by a short spike.

### Assess suitability

Check whether the available text supports at least one clear, interesting, general-audience fact. Prefer recognizable achievements, associations, places, discoveries, works, and historical events over incidental dates, exact statistics, and specialist terminology.

- The importer already excludes many disambiguation and very short abstracts; retain those safeguards.
- Skip navigation/non-content pages and unusable or ambiguous text.
- Do not automatically reject every list article: the presidents list has a substantial factual abstract. Judge the text actually available, not unseen list entries.
- Missing descriptions are acceptable when the abstract is sufficient.
- Flag sensitive subjects and changing claims; prefer stable facts, otherwise require dated wording and verification.
- Allow explicit include/exclude and question-count overrides, with reasons.

For the pilot, inspect the shortlist manually. During generation, ask the model to return either suitable questions or a structured skip reason in the same request. A separate model-scoring pass is not required initially; model suitability judgments remain suggestions subject to review.

### Fixed top-level categories

Use the following closed taxonomy in the initial implementation. `category` stores the stable ID, not an invented label. The generator must select exactly one; it cannot add categories or return multiple categories.

| Category ID | Display name | Scope |
| --- | --- | --- |
| `history` | History | Historical events, civilizations, conflicts, exploration, and social movements |
| `geography` | Geography | Countries, cities, regions, landmarks, physical features, and locations |
| `science_nature` | Science & Nature | Biology, animals, plants, chemistry, physics, astronomy, mathematics, medicine, and the human body |
| `technology` | Technology & Inventions | Computing, engineering, inventions, machines, transportation technology, and communications |
| `film_television` | Film & Television | Movies, TV, actors, screen characters, creators, and screen awards |
| `music` | Music | Performers, songs, albums, instruments, genres, and musical works |
| `literature_language` | Literature & Language | Books, authors, poetry, comics, words, languages, and linguistics |
| `arts_design` | Arts & Design | Visual art, architecture, design, fashion, theater, dance, and other performing arts not assigned to Music or Film & Television |
| `sports` | Sports | Athletic sports, athletes, teams, rules, competitions, and records |
| `games_hobbies` | Games & Hobbies | Board/card/video games, puzzles, toys, collecting, and non-sport recreation; includes esports |
| `food_drink` | Food & Drink | Ingredients, dishes, cuisines, cooking, beverages, and culinary traditions |
| `politics_law` | Politics & Law | Government, civic institutions, political systems, elections, law, courts, and legal cases |
| `religion_philosophy` | Religion & Philosophy | Religions, mythology, beliefs, religious practices, philosophers, and philosophical ideas |
| `business_economics` | Business & Economics | Companies, brands, commerce, money, economic concepts, and business figures |
| `people_society` | People & Society | Personal lives and relationships, social customs, secular holidays, education, occupations, journalism, internet culture, and everyday/household practices |

This is intended to cover the question pool through broad subject domains, not an `Other`, `Miscellaneous`, or `General Knowledge` escape category. The taxonomy is fixed during a generation run. Changes require an explicit code/requirements change and a new taxonomy version, never a model-created category.

**Classify the fact tested, not the source article or the person's identity.** Different questions from the United States article can belong to Geography, History, or Politics & Law. Biographies are not a separate top-level category: career achievements belong to their subject domain; personal-life facts belong to People & Society.

Overlap rules:

- Historical events and their consequences belong to History; how governments or laws work belongs to Politics & Law. Merely asking about an old film, song, or sporting result does not move it into History.
- Scientific explanations and natural phenomena belong to Science & Nature; engineered devices and how they work belong to Technology & Inventions.
- A work or performance belongs to its medium: a novel or manga to Literature & Language, its film/TV/anime adaptation to Film & Television, a stage play to Arts & Design, and a song to Music. Stage musicals and their productions belong to Arts & Design; questions about a song's composition or recording belong to Music.
- Company ownership, founders, and business activity belong to Business & Economics; a product's technical operation belongs to Technology & Inventions.
- Religious beliefs, deities, mythology, and ritual meanings belong to Religion & Philosophy. Secular holidays, social customs, and etiquette belong to People & Society; artistic works and design traditions belong to Arts & Design. Holiday foods belong to Food & Drink when the question tests the food rather than the observance.
- Questions about a celebrity's performances belong to the relevant medium; questions about their personal relationships belong to People & Society. Professional achievements do not move into People & Society just because the answer is a person.
- Sport competitions belong to Sports; board/card/video games, including chess and esports, belong to Games & Hobbies. Health and medical facts belong to Science & Nature, not People & Society.
- Recent events retain their subject category: a recent election is Politics & Law, a new album is Music. Recency is a time-sensitivity flag, not a top-level category. General-knowledge rounds mix categories rather than creating a General Knowledge category.
- People & Society has its stated scope; it is not a fallback for uncertainty. For other overlaps, choose the domain most directly needed to answer the question. Reviewers resolve ambiguous assignments, and unclear questions can be rewritten or rejected rather than multi-tagged. If otherwise valid questions repeatedly have no appropriate home, revise the taxonomy deliberately rather than forcing a misleading label.

### Research check: comparison with established trivia products

Reviewed first-party category lists on 2026-09-25. These are product-specific taxonomies, not evidence that one category count is universally optimal:

| Source | Published structure | Implication for our question bank |
| --- | --- | --- |
| [Hasbro: Trivial Pursuit Classic Edition](https://instructions.hasbro.com/en-us/instruction/trivial-pursuit-game-classic-edition) | Six: Geography, Entertainment, History, Art and Literature, Science and Nature, Sports and Leisure | Our taxonomy covers all six domains, splitting broad entertainment/leisure groups for better control of the question mix. |
| [Etermax: Trivia Crack Classic Mode](https://etermax.com/news/trivia-crack-unveils-extensive-transformation-with-10th-anniversary-upgrade) | Six: Entertainment, Sports, Science, Art, History, Geography | Reinforces the familiar core domains; six suits its game format but need not dictate our storage taxonomy. |
| [Sporcle: category directory](https://www.sporcle.com/categories/) | Fifteen, including separate Movies, Television, Literature, Language, Gaming, Religion, and Holiday, plus Entertainment, Just For Fun, and Miscellaneous | Supports a more granular catalog. We combine some related domains and avoid importing overlapping catch-all or format labels. This is its website directory, not a claim about all Sporcle pub-trivia rounds. |
| [Open Trivia Database: live category list](https://opentdb.com/api_category.php) | Twenty-four, including Politics, Mythology, Celebrities, Animals, Vehicles, Computers, Mathematics, Comics, Anime/Manga, and Cartoons | Our broad domains cover these, but celebrity personal lives and medium-specific works need explicit assignment rules. General Knowledge is a mixed pool, not a missing subject domain. |
| [The Trivia API: live category list](https://the-trivia-api.com/v2/categories) | Ten: Arts & Literature, Film & TV, Food & Drink, General Knowledge, Geography, History, Music, Science, Society & Culture, Sport & Leisure | Direct support for Food & Drink and a social/cultural domain alongside the traditional core. Our stricter divisions avoid an unrestricted General Knowledge bucket. |
| [Water Cooler Trivia: custom-quiz categories](https://www.watercoolertrivia.com/help-questions/can-i-customize-my-trivia-quizzes) | Eight: Sports & Games, Science & Tech, Social Studies, Word Play, Pop Culture, Fine Arts, Current Events, Miscellaneous | Supports technology, language, and social topics; our schema keeps recency and question format separate from subject classification. |

**Decision:** keep fifteen top-level categories, with two refinements from the initial draft: rename Arts & Culture to **Arts & Design**, and Everyday Life & Society to **People & Society**. Separate creative works from customs/social life, explicitly cover popular-culture personal facts, and retain the overlap rules above. Category IDs change now, before implementation; subsequent changes require versioning.

Do not add stand-alone Celebrities, Holidays, Animals, Vehicles, or Anime categories: their facts fit the existing domains, and subcategories provide the useful specialization. Keep Food & Drink separate for audience relevance, and keep Politics & Law, Religion & Philosophy, and Business & Economics separate so selection/review can distinguish them. Those granularity choices are design judgments for this project, not a claimed industry standard. Do not require equal question counts across all fifteen categories.

Coverage spot-checks:

| Example question focus | Assigned category |
| --- | --- |
| An actor's film role / the actor's spouse | Film & Television / People & Society, respectively |
| Thanksgiving's annual observance / a traditional dish | People & Society / Food & Drink, respectively |
| A deity's role in mythology | Religion & Philosophy |
| A building's architect / the city where it stands | Arts & Design / Geography, respectively |
| A car manufacturer's founder / an engine's operation | Business & Economics / Technology & Inventions, respectively |
| A manga character / that character's screen adaptation | Literature & Language / Film & Television, respectively |
| An animal's biology / the country's location in its habitat description | Science & Nature / Geography, respectively |
| A chess rule / an Olympic swimming record | Games & Hobbies / Sports, respectively |
| A medical discovery's scientific significance | Science & Nature |
| A recent court decision's legal effect | Politics & Law |

This desk review supports coverage but does not prove consistent classification or coverage of every future question. During the pilot, explicitly review boundary cases and record category disagreements/corrections before freezing taxonomy v1. The one-category rule is an editorial assignment policy; real-world subjects inevitably overlap.

### Open-ended subcategories

Every question must also have exactly one `subcategory`: a concise, nonempty text label within its category. There is **no predefined subcategory list**; the generator may create broad or specific labels as appropriate, such as `Ancient Egypt`, `US Presidents`, `1980s Pop`, `Human Anatomy`, or `Word Origins`.

Trim surrounding whitespace and use consistent wording/capitalization where practical. Reuse an appropriate existing label when available, but do not restrict generation to previously used labels. Subcategories are scoped to their parent category and are not another enum or a list of tags. Review can consolidate synonymous labels later without adding a third taxonomy level.

### Keep the pool balanced

Assign the required category and subcategory during generation and verify them in review. Article-level classifications used for selection are only provisional; the question's tested fact determines its final classification.

Sample the pilot across categories and popularity bands rather than simply processing the first 100 rows. Balance the approved question pool, not just the source-article count. Pause overrepresented categories and select more from underrepresented ones instead of inventing a complicated combined score. Add out-of-shortlist inclusions when an important category is missing.

## 4. Questions per article

**Current decision: generate at most one question per `page_id`.** This keeps generation, restart behavior, and duplicate prevention simple. The selector's abstract-length ceiling and `question_count_override` column are informational only and are not consumed by the current generator. A broad article is still eligible, but it produces only one independently useful question under this policy.

Multi-question-per-page generation is deferred. If revisited, first identify distinct, independently supported facts and check for overlap; abstract length alone must never trigger extra questions. The database's current unique `page_id` constraint and job lifecycle would also need a deliberate migration.

## 5. Generate through one model-independent contract

Keep the prompt and model choice open. Define an ordinary JSON input/output contract usable by a hosted model or a local runner such as Ollama. Implement only the backend we choose first; no plugin framework is needed.

Input: one article's title, description, abstract and source identifiers, plus audience/style instructions and prompt version. The current generation contract requests no more than one question for that `page_id`.

For each question, require the content fields `category`, `subcategory`, `level`, `question`, `a`, `b`, `c`, `d`, `metadata`, and optional `funfact`, with the following contract:

- `category`: exactly one allowed hard-coded ID; `subcategory`: one nonempty free-text label; `level`: actual integer 1–9.
- `question`: a complete, self-contained stem. It must remain clear with the category, subcategory, article title, and prior questions hidden; name the subject or resolve every reference within the stem. Do not rely on orphaned phrases such as “this war,” “the movement,” or “the company.” It must not contain or give away the answer (see the leakage rules below).
- `a`: always the correct answer. `b`, `c`, and `d`: distinct, incorrect, plausible distractors. There is no second answer key: correctness is defined by `a`.
- `metadata`: a required JSON object. Require `fact_tested`, a concise `explanation`, and `evidence` containing the exact supporting source passage; optional flags such as sensitivity and time-sensitivity can also live here. Keep model/prompt/raw-response provenance in `generation_attempts`, not duplicated in this object. The game must not expose metadata during the question phase.
- `funfact`: optional follow-up fact, shown only after the answer is revealed. It must be supported by the same saved source; retain its supporting passage in metadata (for example, `funfact_evidence`) when present.

The application, not the model, assigns `id`, `page_id`, `created_at`, and `updated_at` when it persists an accepted question. `page_id` comes from the selected article job and must not be trusted from model output. Generate `id` as a unique UUID version 4. Set UTC timestamps on creation and update `updated_at` whenever the question record changes; the model cannot choose either timestamp.

The correct answer and explanation must be supported by the supplied text. Distractors must be plausible and unambiguously wrong; the source passage alone will not always prove that, so review must check them. Do not silently expand beyond the abstract using the model's memory. Mark insufficient evidence for review or skip it.

### No answer leakage; plausible distractors

The question stem must not state, repeat, translate, paraphrase, or otherwise disclose the correct answer or the exact fact it asks the player to retrieve. Do not put the answer in a lead-in, quoted source fact, parenthetical, article title, clue, or other framing and then ask for that same answer. In particular, reject patterns such as:

- Giving a translation, then asking for that translation.
- Naming the phenomenon in the description (for example, a hurricane's wind speed), then asking what kind of phenomenon it was.
- Stating the year in the setup, then asking which year it was.

A stem may include relevant clues, but it must require the player to infer or recall the answer rather than merely repeat a fact already given. Avoid making the right option conspicuously longer, more specific, grammatical, or differently formatted than its distractors. Make distractors credible, parallel in type/specificity/format, and clearly wrong; no second option may also be defensible.

Check leakage automatically for exact and normalized answer-string occurrences in the stem (case, punctuation, whitespace, and common formatting variants). These checks only catch obvious cases; they cannot reliably catch synonyms, translations, aliases, or semantic restatements. Flag suspected semantic leakage for review, and require a human reviewer to read the stem and all four choices in randomized order without a marked correct choice, then decide whether the stem discloses the answer or the choices make it virtually unavoidable. Rejection or rewriting is preferable to trying to rescue a flawed question with harder distractors.

Storage invariant: `a` always contains the correct answer. **Never display the stored field labels as the answer positions in a game.** Randomize the four choices for each play/session (and map correctness to the displayed choice), so the correct stored answer does not always appear as the player's first/“A” option. Keep this presentation mapping out of the question stem and apply it consistently to any review/game client.

### Required difficulty level: 1–9

Assess how difficult the **complete multiple-choice question, including all four answer choices**, would be for an average US adult answering individually without looking up the answer or reading the source abstract. Consider a general adult population across ages and educational backgrounds, not enthusiasts, specialists, or only experienced trivia players.

| `level` | Meaning for that audience |
| --- | --- |
| 1 | Near-universal knowledge: almost any adult would answer correctly. |
| 2 | Very easy: familiar knowledge the overwhelming majority would get right. |
| 3 | Easy: most adults would answer correctly without much thought. |
| 4 | Accessible: more adults than not would likely succeed through knowledge or straightforward elimination. |
| 5 | Moderate: mixed results; requires some recall, thought, or familiarity with the subject. |
| 6 | Challenging: a minority would confidently know the answer; some subject knowledge is helpful. |
| 7 | Hard: relatively few adults would know it; usually requires a strong interest in the subject. |
| 8 | Very hard: uncommon or detailed knowledge; few non-specialists would answer confidently. |
| 9 | Extremely hard: difficult for the vast majority, even with the choices; usually specialist or exceptionally uncommon knowledge. |

Rules for assigning levels:

- Rate the specific fact, wording, clues, and distractors together. Recognizing the subject is not the same as knowing the answer.
- Do not derive level mechanically from popularity rank, article length, category, or how confident the generating model feels.
- Plausible distractors can make a question harder; obvious distractors or helpful clues can make it easier. Bad wording, trickery, ambiguity, and unsupported facts are quality defects, not valid ways to earn a high level.
- These are qualitative initial estimates, not measured correctness percentages. Four-choice guessing alone succeeds 25% of the time, so level 9 does not mean a near-zero raw success rate.
- Reviewers must assess and may change the assigned level. Changes to wording or answer choices require reassessing level. Later player results should calibrate the rubric, accounting for audience and guessing.
- All nine levels are valid. Do not force an equal distribution; selecting suitable levels for a particular pub-trivia set is separate from honestly rating each question.

### Enforce the classification contract

Hard-code the category IDs/scopes and the difficulty rubric in the generation process. Maintain one canonical category definition in code and use it to supply the prompt, the JSON schema's `category` enum, and validation/storage constraints. Both hosted and local models receive the same definitions, overlap rules, and level rubric. Record taxonomy and difficulty-rubric versions with generation metadata.

Require `category`, `subcategory`, and `level` in every question response and review import. Reject missing/unknown/multiple categories, empty or whitespace-only subcategories, and levels that are not actual integers in the inclusive range 1–9; do not silently substitute defaults, round values, or coerce strings/booleans. Retain invalid output in the attempt log for bounded repair/retry, not as a valid question draft.

Enforce the same invariants in SQLite: required category constrained to the allowed IDs, required nonempty subcategory text, and a required integer level constrained to 1–9. A range check alone is not enough for SQLite's flexible typing. The review/export path must preserve these fields and cannot bypass validation.

Treat source text as data, not instructions, and validate all returned JSON. Store the exact prompt/request, model identity/settings, raw response, validation result, timestamps, and usage/cost when available. Never store credentials.

## 6. Store workflow state separately from source data

Recommend a new **`data/trivia.sqlite`**, leaving `data/wikipedia.sqlite` as a read-only source. Copy only selected source snapshots, not the full corpus. This makes question editing and backups independent of future Wikipedia imports.

Four initial tables are enough:

| Table | Purpose |
| --- | --- |
| `article_jobs` | One selected article snapshot/provenance per `page_id`, job status, and completion/skip reason |
| `generation_attempts` | Each model attempt, linked to its job; exact request/response, model/prompt information, errors, timing, and available usage |
| `questions` | The current question record, with required fields directly on it: UUIDv4 `id`, source `page_id`, `category`, `subcategory`, integer `level`, `question`, correct `a`, distractors `b`/`c`/`d`, JSON `metadata`, nullable `funfact`, and UTC `created_at`/`updated_at`; also review status and job/attempt links |
| `question_revisions` | Immutable snapshots of prior edits/reviewed versions, preserving the question fields and that version's review decision/notes/reviewer/time; the stable question UUID remains the parent link |

Use canonical UUIDv4 strings as unique `questions.id` values; allocate them when saving (for example, with Python's standard-library UUID support), not from model output. Use `page_id INTEGER NOT NULL` directly on every question. In the current source schema, `articles.page_id` is the primary key, so it is the unique join key to the original Wikipedia article. Validate it against `articles` before saving, and retain the source title, revision ID, supplied text, URL, and license in the related job snapshot because page ID identifies a page, not the exact text revision used.

Because `trivia.sqlite` and `wikipedia.sqlite` are separate databases, SQLite cannot enforce a normal foreign key from question `page_id` to `articles.page_id`. Enforce that link during selection/import and preserve the source snapshot; keep ordinary foreign keys among tables inside `trivia.sqlite`.

Store `metadata` as valid JSON text containing an object, not a second source of truth for the top-level required fields. Enforce category membership, actual integer level 1–9, nonempty subcategory, required answer/stem/options/timestamps, and UUID uniqueness when saving. Keep `funfact` nullable. Set timestamps in the application in UTC, in one documented ISO-8601 format.

Preserve attribution through exports and check applicable CC BY-SA obligations before distribution.

### Article generation lifecycle

`queued → running → generated | skipped | failed`

- `generated`: valid drafts have been saved; it does **not** mean approved.
- `skipped`: intentionally no questions, with a reason.
- `failed`: technical failure, eligible for a bounded retry or manual investigation.
- A completed job has at most one saved question; skipped and failed jobs retain their reasons and attempt history.

The current schema keys `article_jobs` by `page_id` and enforces one `questions` row per page. Reruns skip done/skipped jobs, and retries require `--retry-failed`; changed source snapshots are rejected. Deliberate regeneration/multiple generation passes are not currently supported.

Start with one worker. Commit saved output and the job's terminal state together. Recover interrupted `running` jobs explicitly on restart, with bounded retries. Local persistence must be idempotent; a crash around an external API response may still require another paid call unless that provider supports idempotency.

### Question review lifecycle

`draft → approved | needs_edit | rejected`

Editing creates a new draft revision and requires approval again. Archive the previous question fields and review decision in `question_revisions` before updating the direct fields on `questions`; retain the same UUID and `page_id`, and refresh `updated_at`. Approval belongs to the exact revision reviewed; it must not silently carry over to changed stem, answer, distractors, metadata/evidence, fun fact, category, subcategory, or level. Preserve rejected drafts and reasons to improve later prompts.

Reports should show separate totals for selected articles, generated/skipped/failed jobs, drafted questions, and approved/rejected/questions-needing-edit, with question counts broken down by category, subcategory, and level. An article's “completed” flag cannot represent all of these.

## 7. Validate, review, and export

Run inexpensive automatic checks before human review:

- Persist the required question columns: UUIDv4 unique `id`; valid source `page_id`; exactly one allowed category; nonempty text subcategory; integer level 1–9; nonempty `question`/`a`/`b`/`c`/`d`; valid JSON-object `metadata`; nullable text `funfact`; UTC `created_at`/`updated_at`.
- Verify `a` is factually correct from the saved source and `b`/`c`/`d` are distinct, plausible, and unambiguously incorrect; there is no separate correct-answer field.
- Require metadata explanation and evidence; verify the evidence passage appears in the saved source, allowing whitespace normalization. If `funfact` is present, validate its support too.
- Exact/normalized answer-in-stem checks for obvious leakage; flag potential paraphrase, translation, title/alias leakage, and answer giveaways for semantic/human review.
- Exact duplicate detection using normalized question text; flag possible repeated facts for review, including across different articles.
- Suspicious answer leakage or time-dependent wording should be flagged, not treated as reliably solved by simple rules.

Humans review factual support, whether only one choice is correct, distractor quality, whether the stem gives away the correct answer, and whether it is understandable when shown by itself without its article title, category, or subcategory. Also review wording, the 1–9 level for an average US adult, interest, category/subcategory assignment and balance, duplication, and sensitive/time-sensitive material. Automated/model review can assist later but does not prove correctness. Required difficulty levels are estimates until tested with actual players. Render choices in a randomized order during play; never disclose that stored field `a` is the answer.

Start with a simple review export/import carrying stable question and revision IDs; reject imports against an outdated revision. A dedicated review UI can wait. Export only the current approved revision of a question, with provenance and attribution retained.

Source refreshes must not overwrite the evidence used for existing questions. Flag affected questions for reassessment; any replacement question becomes a new draft revision rather than silently changing approved content.

## 8. Roll out in small stages

1. **Selection report, no model calls — implemented:** `select_trivia_articles.py` reads the source database read-only and writes the ranked top-10,000 candidate manifest to `data/trivia_candidates.csv`. The CSV includes source snapshots/provenance, popularity and length signals, suggested question ceilings, review flags, and editable `include_in_pilot`, `question_count_override`, and `review_notes` columns. Existing manifests are not overwritten by default. Article categories are intentionally not guessed because classification belongs to each generated question.
2. **Curate the pilot list — generated:** `data/trivia_candidates.csv` marks 100 articles with `include_in_pilot=1`, sampled across the rank bands (20 ranks 1–100; 34 ranks 101–1,000; 36 ranks 1,001–5,000; 10 ranks 5,001–10,000). Those 100 produced one draft each. Subsequent ranked generation expanded the local database to 814 questions from 814 distinct pages. All remain drafts; the source Wikipedia database is read-only. The selector's question-count ceilings and overrides are informational under the current one-question-per-page policy.
3. **Review every pilot question:** measure approval rate, rejection reasons, questions per article, duplicates, category/subcategory mix, the level 1–9 distribution, reviewer corrections to classification/level, review time, generation time, and cost per approved question. Allow a smaller matched subset to compare hosted and local models later.
4. **Tune selection rules, length ceilings, and prompt:** calibrate using the pilot rather than choosing a complex scoring formula now.
5. **Expand in bounded batches:** aim for the first 1,000 approved questions, then broaden coverage only if quality and review effort are acceptable.

Before scaling, add runnable checks for count boundaries, malformed model output, duplicate prevention, interrupted-job recovery, and the rule that edits invalidate approval. Field-contract checks must generate/validate unique UUIDv4 IDs, require a real source `page_id`, enforce the required question fields and types (allowing separately defined internal workflow columns), enforce `a` as the correct stored answer and `b`/`c`/`d` as distinct distractors, validate metadata JSON, and set/update timestamps. Classification checks must accept every defined category, reject unknown/missing/multiple categories and empty/whitespace-only subcategories, accept levels 1 and 9, and reject out-of-range or non-integer levels (including strings and booleans). Leakage checks must catch the three stated examples and normalized direct answer repeats, while routing semantic cases to human review. Verify generation, review imports, storage, revisions, and exports preserve the fields; verify game presentation randomizes the stored options without losing the answer mapping.

**Current status:** the generator, separate trivia database, and bounded durable worker spool are implemented; 814 draft questions are stored locally. The agreed generation policy is one question per `page_id`. A formal review/approval/export workflow remains future work; do not treat draft status as production approval.
