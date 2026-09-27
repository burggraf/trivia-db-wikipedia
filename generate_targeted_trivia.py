#!/usr/bin/env python3
"""Generate category-targeted trivia drafts without changing standard article jobs on misses."""

import argparse
import csv
import fcntl
import json
import sqlite3
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import generate_trivia as base

CATEGORY_HINTS = {
    'food_drink': ('food', 'drink', 'restaurant', 'cuisine', 'recipe', 'beer', 'wine', 'coffee', 'tea',
                   'cocktail', 'pizza', 'chocolate', 'cheese', 'bread', 'fruit', 'vegetable'),
    'games_hobbies': ('game', 'chess', 'poker', 'board game', 'video game', 'card game', 'toy', 'hobby',
                      'puzzle', 'role playing'),
    'arts_design': ('art', 'artist', 'painting', 'museum', 'architecture', 'architect', 'sculpture',
                    'photography', 'design', 'theatre', 'theater', 'dance'),
    'religion_philosophy': ('religion', 'philosophy', 'church', 'mosque', 'temple', 'buddh', 'islam',
                            'christian', 'judaism', 'hindu', 'bible', 'quran', 'mythology'),
    'business_economics': ('business', 'company', 'corporation', 'bank', 'economy', 'economic', 'market',
                           'finance', 'stock', 'investment', 'currency', 'trade', 'retail', 'industry'),
    'people_society': ('people', 'society', 'social', 'culture', 'population', 'family', 'education', 'human',
                       'community', 'fashion', 'marriage', 'demographic'),
    'literature_language': ('literature', 'book', 'novel', 'author', 'poem', 'poetry', 'writer', 'language',
                            'linguistic', 'alphabet', 'dictionary', 'grammar'),
    'technology': ('technology', 'software', 'computer', 'internet', 'programming', 'device', 'digital',
                   'hardware', 'smartphone', 'operating system', 'web', 'database', 'artificial intelligence'),
    'science_nature': ('science', 'scientist', 'biology', 'physics', 'chemistry', 'astronomy', 'animal', 'plant',
                       'species', 'nature', 'medicine', 'disease', 'climate', 'geology', 'ecology', 'evolution'),
    'politics_law': ('politics', 'political', 'president', 'senator', 'parliament', 'government', 'law', 'court',
                     'constitution', 'election', 'congress', 'legislation', 'justice', 'legal', 'minister'),
    'geography': ('geography', 'river', 'mountain', 'country', 'city', 'island', 'continent', 'ocean', 'province',
                  'region', 'capital', 'state', 'border', 'desert', 'lake', 'volcano'),
    'sports': ('sport', 'athlete', 'team', 'football', 'basketball', 'baseball', 'soccer', 'tennis', 'olympic',
               'championship', 'league', 'player', 'racing', 'boxing', 'golf', 'cricket'),
    'music': ('music', 'musician', 'singer', 'song', 'album', 'band', 'composer', 'orchestra', 'instrument',
              'guitar', 'piano', 'opera', 'rapper', 'recording'),
    'history': ('history', 'historic', 'historical', 'war', 'battle', 'empire', 'ancient', 'revolution', 'dynasty',
                'king', 'queen', 'civilization', 'treaty', 'archaeology'),
}
RATES = base.RATES


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def parse_categories(value):
    categories = tuple(dict.fromkeys(part.strip() for part in value.split(',') if part.strip()))
    if not categories or any(category not in CATEGORY_HINTS for category in categories):
        raise argparse.ArgumentTypeError(f'categories must be comma-separated IDs from {", ".join(CATEGORY_HINTS)}')
    return categories


def matches_category(article, category):
    tokens = base.normalized_tokens(f'{article.get("title", "")} {article.get("description", "")} {article.get("abstract", "")}')
    return any(base.contains_phrase(tokens, base.normalized_tokens(hint)) for hint in CATEGORY_HINTS[category])


def create_schema(conn):
    base.create_schema(conn)
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS targeted_attempts (
            artifact_id TEXT PRIMARY KEY, page_id INTEGER NOT NULL, target_category TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('done','no_match','failed','superseded')),
            prompt TEXT NOT NULL, raw_response TEXT, quality_review TEXT, error TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0, cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE(page_id, target_category)
        );
        CREATE TABLE IF NOT EXISTS targeted_spool_imports (
            id TEXT PRIMARY KEY, imported_at TEXT NOT NULL
        );
    ''')
    if 'quality_review' not in {row[1] for row in conn.execute('PRAGMA table_info(targeted_attempts)')}:
        conn.execute('ALTER TABLE targeted_attempts ADD COLUMN quality_review TEXT')


def write_spool_result(spool_dir, result):
    artifact = dict(result, page_id=int(result['article']['page_id']))
    return base.write_spool_result(spool_dir, artifact)


def add_counts(total, new):
    return [left + right for left, right in zip(total, new)]


def article_from_artifact(artifact):
    article = artifact.get('article')
    if not isinstance(article, dict):
        raise ValueError('targeted artifact is missing its source article')
    required = ('page_id', 'title', 'abstract', 'source_revision_id')
    if any(not article.get(key) for key in required):
        raise ValueError('targeted artifact has an incomplete source snapshot')
    return article


def save_question(conn, article, question):
    stamp = base.now()
    conn.execute('''INSERT INTO article_jobs(page_id,title,source_revision_id,source_abstract,source_description,source_url,
                    source_modified_at,source_license_json,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                 (int(article['page_id']), article['title'], int(article['source_revision_id']), article['abstract'],
                  article.get('description'), article.get('url'), article.get('source_modified_at'),
                  article.get('license_json'), 'done', stamp))
    conn.execute('''INSERT INTO questions(id,page_id,category,subcategory,level,question,a,b,c,d,metadata,funfact,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                 (str(uuid.uuid4()), int(article['page_id']), question['category'], question['subcategory'], question['level'],
                  question['question'], *(question[key] for key in 'abcd'),
                  json.dumps({**question['metadata'], 'review_status': 'draft', 'human_review_required': True}, ensure_ascii=False),
                  question.get('funfact'), stamp, stamp))


def ingest_spool(conn, spool_dir):
    counts = [0, 0, 0]  # done, no_match, failed
    spool_dir = Path(spool_dir)
    if not spool_dir.exists():
        return tuple(counts)
    for path in sorted(spool_dir.glob('*.json')):
        artifact = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(artifact.get('id'), str) or not artifact['id']:
            raise ValueError(f'invalid targeted spool artifact: {path}')
        if conn.execute('SELECT 1 FROM targeted_spool_imports WHERE id=?', (artifact['id'],)).fetchone():
            path.unlink()
            continue
        target_category = artifact.get('target_category')
        if target_category not in CATEGORY_HINTS:
            raise ValueError(f'invalid target category in spool artifact: {path}')
        article = article_from_artifact(artifact)
        page_id = int(article['page_id'])
        raw = artifact.get('raw_response')
        usage = artifact.get('usage') or {}
        quality_review = artifact.get('quality_review')
        error = artifact.get('error')
        prior_attempt = conn.execute('SELECT 1 FROM targeted_attempts WHERE page_id=? AND target_category=?',
                                    (page_id, target_category)).fetchone()
        global_job = conn.execute('SELECT 1 FROM article_jobs WHERE page_id=?', (page_id,)).fetchone()
        question = None
        if prior_attempt or global_job:
            status = 'superseded'
            error = f'Discarded target result; page_id={page_id} is already resolved'
        else:
            try:
                if error:
                    raise RuntimeError(error)
                item = json.loads(raw)
                if isinstance(item, dict) and set(item) == {'skip_reason'} and isinstance(item['skip_reason'], str) and item['skip_reason'].strip():
                    status = 'no_match'
                    error = item['skip_reason']
                else:
                    candidate = base.validate(item, article)
                    if candidate['category'] != target_category:
                        status = 'no_match'
                        error = f"model selected {candidate['category']}, not requested {target_category}"
                    elif quality_review is not None and (not isinstance(quality_review, dict) or quality_review.get('accepted') is not True):
                        status = 'no_match'
                        error = quality_review.get('reason', 'uniqueness review did not approve') if isinstance(quality_review, dict) else 'invalid uniqueness review'
                    else:
                        question = candidate
                        status = 'done'
            except (ValueError, RuntimeError) as exc:
                status = 'failed'
                error = str(exc)
        input_tokens = int(usage.get('input', 0))
        cached = int(usage.get('cacheRead', 0))
        output_tokens = int(usage.get('output', 0))
        cost = (input_tokens * RATES['input'] + cached * RATES['cacheRead'] + output_tokens * RATES['output']) / 1_000_000
        with conn:
            if question:
                save_question(conn, article, question)
            if not prior_attempt:
                conn.execute('''INSERT INTO targeted_attempts(artifact_id,page_id,target_category,status,prompt,raw_response,quality_review,error,
                                input_tokens,cached_input_tokens,output_tokens,estimated_cost_usd,created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                             (artifact['id'], page_id, target_category, status, artifact['prompt'], raw,
                              json.dumps(quality_review, ensure_ascii=False) if quality_review is not None else None,
                              error, input_tokens, cached, output_tokens, cost, base.now()))
            conn.execute('INSERT INTO targeted_spool_imports(id,imported_at) VALUES(?,?)', (artifact['id'], base.now()))
        path.unlink()
        if status != 'superseded':
            counts[{'done': 0, 'no_match': 1, 'failed': 2}[status]] += 1
    return tuple(counts)


def candidate_rows(conn, rows, category, retry_failed):
    global_pages = {row[0] for row in conn.execute('SELECT page_id FROM article_jobs')}
    statuses = ('done', 'no_match', 'superseded') if retry_failed else ('done', 'no_match', 'failed', 'superseded')
    placeholders = ','.join('?' for _ in statuses)
    attempted = {row[0] for row in conn.execute(
        f'SELECT page_id FROM targeted_attempts WHERE target_category=? AND status IN ({placeholders})',
        (category, *statuses))}
    candidates = []
    seen_page_ids = set()
    for row in rows:
        page_id = int(row['page_id'])
        if page_id not in seen_page_ids and page_id not in global_pages and page_id not in attempted and matches_category(row, category):
            candidates.append(row)
            seen_page_ids.add(page_id)
    return candidates


def generate_to_spool(article, category, generate, spool_dir):
    prompt = base.make_prompt(article, target_category=category)
    raw = None
    usage = {}
    error = None
    quality_review = None
    try:
        raw, usage = generate(article)
        usage = dict(usage or {})
        quality_review = usage.pop('quality_review', None)
    except Exception as exc:
        error = str(exc)
    return write_spool_result(spool_dir, {
        'id': str(uuid.uuid4()), 'target_category': category, 'article': article,
        'prompt': prompt, 'raw_response': raw, 'quality_review': quality_review, 'usage': usage, 'error': error,
    })


def run_category(conn, rows, category, generate, *, target_total, workers, spool_dir, retry_failed=False):
    counts = list(ingest_spool(conn, spool_dir))
    candidates = iter(candidate_rows(conn, rows, category, retry_failed))
    exhausted = False

    def submit_one(executor):
        nonlocal exhausted
        if exhausted:
            return None
        try:
            article = next(candidates)
        except StopIteration:
            exhausted = True
            return None
        return executor.submit(generate_to_spool, article, category, generate, spool_dir)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = set()
        while True:
            done_total = conn.execute("SELECT COUNT(*) FROM targeted_attempts WHERE target_category=? AND status='done'", (category,)).fetchone()[0]
            while done_total + len(futures) < target_total and len(futures) < workers:
                future = submit_one(executor)
                if not future:
                    break
                futures.add(future)
            if not futures:
                break
            complete, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in complete:
                futures.remove(future)
                future.result()
            counts = add_counts(counts, ingest_spool(conn, spool_dir))
    return tuple(counts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('data/trivia_candidates.csv'))
    parser.add_argument('--db', type=Path, default=Path('data/trivia.sqlite'))
    parser.add_argument('--categories', type=parse_categories, required=True)
    parser.add_argument('--per-category', type=positive, default=25)
    parser.add_argument('--workers', type=positive, default=8)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--spool-dir', type=Path, help='default: <db>.targeted.spool')
    parser.add_argument('--pi', default='pi')
    args = parser.parse_args()
    if not args.manifest.is_file():
        parser.error('manifest not found')
    if args.db.resolve() in (args.manifest.resolve(), Path('data/wikipedia.sqlite').resolve()):
        parser.error('refusing to write to the manifest or source Wikipedia database')
    args.db.parent.mkdir(parents=True, exist_ok=True)
    lock_path = args.db.with_suffix(args.db.suffix + '.lock')
    spool_dir = args.spool_dir or args.db.with_suffix(args.db.suffix + '.targeted.spool')
    with lock_path.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('another trivia generator is using this database')
        with sqlite3.connect(args.db) as conn, args.manifest.open(newline='', encoding='utf-8') as file:
            conn.execute('PRAGMA foreign_keys=ON')
            create_schema(conn)
            rows = list(csv.DictReader(file))
            for category in args.categories:
                done_before = conn.execute("SELECT COUNT(*) FROM targeted_attempts WHERE target_category=? AND status='done'", (category,)).fetchone()[0]
                target_total = done_before + args.per_category
                counts = run_category(conn, rows, category,
                                      lambda article: base.pi_generate(article, executable=args.pi, target_category=category),
                                      target_total=target_total, workers=args.workers, spool_dir=spool_dir,
                                      retry_failed=args.retry_failed)
                done_after = conn.execute("SELECT COUNT(*) FROM targeted_attempts WHERE target_category=? AND status='done'", (category,)).fetchone()[0]
                print(f'{category}: done={counts[0]} no_match={counts[1]} failed={counts[2]}; targeted total={done_after}')
                if done_after < target_total:
                    parser.error(f'only added {done_after - done_before}/{args.per_category} suitable {category} candidates')


if __name__ == '__main__':
    main()
