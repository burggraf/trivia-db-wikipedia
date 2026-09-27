import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_trivia as g


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(Path(self.tmp.name) / 'trivia.sqlite')
        self.addCleanup(self.conn.close)
        g.create_schema(self.conn)
        self.rows = [dict(page_id=str(n), title=f'Article {n}', abstract=f'Article {n} was founded in Paris.',
                          source_revision_id=str(n + 10), url=f'https://example.org/{n}',
                          description='Test article', rank=str(n), include_in_pilot='1') for n in (1, 2)]

    def response(self, row):
        return dict(category='geography', subcategory='Cities', level=2,
                    question=f'In which city was {row["title"]} founded?',
                    a='Paris', b='London', c='Rome', d='Madrid',
                    metadata=dict(fact_tested='City of founding', evidence=f'{row["title"]} was founded in Paris.',
                                  explanation='The source names Paris.'), funfact=None)

    def test_prompt_includes_honest_nine_level_rubric_without_quotas(self):
        prompt = g.make_prompt(self.rows[0])
        for marker in (
            '1: Near-universal knowledge', '2: Very easy', '3: Easy',
            '4: Accessible', '5: Moderate', '6: Challenging', '7: Hard',
            '8: Very hard', '9: Extremely hard',
            'Rate the complete multiple-choice question',
            'do not force an equal distribution',
        ):
            self.assertIn(marker, prompt)

    def test_rejects_duplicate_page_ids_before_dispatch(self):
        with self.assertRaisesRegex(ValueError, 'duplicate page_id'):
            g.run_batch(self.conn, [self.rows[0], self.rows[0]], lambda _: self.fail('must not dispatch'),
                        limit=2, workers=2, spool_dir=Path(self.tmp.name) / 'spool')
        self.assertEqual(self.conn.execute('select count(*) from article_jobs').fetchone()[0], 0)

    def test_ingests_spool_before_dispatch(self):
        g.claim_job(self.conn, self.rows[0], retry_failed=False)
        spool = Path(self.tmp.name) / 'spool'
        g.write_spool_result(spool, dict(id='recovery', page_id=1, source_revision_id=11,
                                         prompt='test', raw_response=json.dumps(self.response(self.rows[0])),
                                         usage={}, error=None))
        self.assertEqual(g.run_batch(self.conn, self.rows[:1], lambda _: self.fail('must recover without regenerating'),
                                     limit=1, spool_dir=spool), (1, 0, 0))

    def test_dispatches_up_to_worker_limit_before_importing(self):
        started = threading.Barrier(2, timeout=1)
        calls = []
        def generate(row):
            calls.append(row['page_id'])
            started.wait()
            return json.dumps(self.response(row)), {}
        spool = Path(self.tmp.name) / 'spool'
        self.assertEqual(g.run_batch(self.conn, self.rows, generate, limit=2, workers=2, spool_dir=spool), (2, 0, 0))
        self.assertCountEqual(calls, ['1', '2'])
        self.assertEqual(self.conn.execute('select count(*) from generation_attempts').fetchone()[0], 2)

    def test_resume_skips_done_and_keeps_stable_question(self):
        calls = []
        def generate(row):
            calls.append(row['page_id'])
            return json.dumps(self.response(row)), dict(input=100, output=50, cacheRead=20)
        self.assertEqual(g.run_batch(self.conn, self.rows, generate, limit=1), (1, 0, 0))
        first = self.conn.execute('select id from questions where page_id=1').fetchone()[0]
        self.assertEqual(g.run_batch(self.conn, self.rows, generate, limit=1), (1, 0, 0))
        self.assertEqual(calls, ['1', '2'])
        self.assertEqual(g.run_batch(self.conn, self.rows, generate, limit=1), (0, 0, 0))
        self.assertEqual(first, self.conn.execute('select id from questions where page_id=1').fetchone()[0])
        self.assertEqual(self.conn.execute('select count(*) from generation_attempts').fetchone()[0], 2)

    def test_failure_persisted_and_retry_is_explicit(self):
        calls = []
        def generate(row):
            calls.append(row['page_id'])
            if len(calls) == 1:
                raise RuntimeError('model unavailable')
            return json.dumps(self.response(row)), dict(input=3, output=4, cacheRead=0)
        self.assertEqual(g.run_batch(self.conn, self.rows[:1], generate, limit=1), (0, 0, 1))
        self.assertEqual(g.run_batch(self.conn, self.rows[:1], generate, limit=1), (0, 0, 0))
        self.assertEqual(g.run_batch(self.conn, self.rows[:1], generate, limit=1, retry_failed=True), (1, 0, 0))
        self.assertEqual(len(calls), 2)
        self.assertEqual([r[0] for r in self.conn.execute('select status from generation_attempts order by id')], ['failed', 'done'])

    def test_recover_interrupted_and_skip_no_usable_fact(self):
        g.claim_job(self.conn, self.rows[0], retry_failed=False)
        self.assertEqual(g.run_batch(self.conn, self.rows[:1], lambda _: ('{"skip_reason":"No suitable fact"}', {}), limit=1), (0, 1, 0))
        self.assertEqual(self.conn.execute('select status from article_jobs where page_id=1').fetchone()[0], 'skipped')

    def test_pi_sends_large_prompt_on_stdin_not_argv(self):
        event = {'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop',
                 'content': [{'type': 'text', 'text': '{"skip_reason":"short"}'}],
                 'usage': {'input': 6, 'output': 3}}}
        fake = type('Result', (), {'stdout': json.dumps(event) + '\n', 'stderr': '', 'returncode': 0})()
        with patch.object(g.subprocess, 'run', return_value=fake) as run:
            text, usage = g.pi_generate(self.rows[0])
        self.assertEqual(text, '{"skip_reason":"short"}')
        self.assertEqual(usage['input'], 6)
        self.assertIn('Article 1', run.call_args.kwargs['input'])
        self.assertNotIn('Article 1', ' '.join(run.call_args.args[0]))

    def test_retry_rejects_changed_source_snapshot(self):
        self.assertEqual(g.run_batch(self.conn, self.rows[:1], lambda _: ('not json', {}), limit=1), (0, 0, 1))
        changed_revision = [dict(self.rows[0], source_revision_id='999')]
        with self.assertRaisesRegex(ValueError, 'source revision changed'):
            g.run_batch(self.conn, changed_revision, lambda _: ('{}', {}), limit=1, retry_failed=True)
        changed_abstract = [dict(self.rows[0], abstract='Article 1 was founded in Rome.')]
        with self.assertRaisesRegex(ValueError, 'source snapshot changed'):
            g.run_batch(self.conn, changed_abstract, lambda _: self.fail('must reject stale manifest'),
                        limit=1, retry_failed=True)

    def test_spool_result_is_atomic_and_synced(self):
        spool = Path(self.tmp.name) / 'spool'
        result = dict(id='artifact-1', page_id=1, source_revision_id=11,
                      prompt='test prompt', raw_response=json.dumps(self.response(self.rows[0])),
                      usage=dict(input=3, output=4), error=None)
        with patch.object(g.os, 'fsync', wraps=g.os.fsync) as sync:
            path = g.write_spool_result(spool, result)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')), result)
        self.assertGreaterEqual(sync.call_count, 2)  # file content and renamed directory entry
        self.assertFalse(list(spool.glob('*.tmp')))

    def test_duplicate_spool_results_do_not_block_recovery(self):
        self.conn.execute("insert into article_jobs(page_id,title,source_revision_id,source_abstract,status) values(1,'Article 1',11,'Article 1 was founded in Paris.','running')")
        self.conn.commit()
        spool = Path(self.tmp.name) / 'spool'
        for artifact_id in ('artifact-a', 'artifact-b'):
            g.write_spool_result(spool, dict(id=artifact_id, page_id=1, source_revision_id=11,
                                             prompt='test prompt', raw_response=json.dumps(self.response(self.rows[0])),
                                             usage={}, error=None))
        self.assertEqual(g.ingest_spool(self.conn, spool), (1, 0, 0))
        self.assertEqual(self.conn.execute('select count(*) from questions').fetchone()[0], 1)
        self.assertEqual(self.conn.execute("select status from generation_attempts order by id").fetchall(), [('done',), ('superseded',)])
        self.assertFalse(list(spool.glob('*.json')))

    def test_spool_import_is_idempotent_after_commit_before_delete(self):
        self.conn.execute("insert into article_jobs(page_id,title,source_revision_id,source_abstract,status) values(1,'Article 1',11,'Article 1 was founded in Paris.','running')")
        self.conn.commit()
        spool = Path(self.tmp.name) / 'spool'
        result = dict(id='artifact-1', page_id=1, source_revision_id=11,
                      prompt='test prompt', raw_response=json.dumps(self.response(self.rows[0])),
                      usage=dict(input=3, output=4), error=None)
        g.write_spool_result(spool, result)
        self.assertEqual(g.ingest_spool(self.conn, spool), (1, 0, 0))
        self.assertEqual(self.conn.execute('select count(*) from questions').fetchone()[0], 1)
        g.write_spool_result(spool, result)  # simulate a crash after commit but before unlink
        self.assertEqual(g.ingest_spool(self.conn, spool), (0, 0, 0))
        self.assertEqual(self.conn.execute('select count(*) from generation_attempts').fetchone()[0], 1)
        self.assertFalse(list(spool.glob('*.json')))

    def test_distinguishes_choices_with_meaningful_punctuation(self):
        item = self.response(self.rows[0])
        item.update(a='Java', b='C++', c='C#', d='Python')
        g.validate(item, self.rows[0])

    def test_rejects_short_and_formatted_answer_leaks_without_substring_false_positives(self):
        for answer, stem in (
            ('US', 'Which currency is used in the US?'),
            ('C++', 'Which programming language is C++?'),
            ('São Paulo', 'Which city is Sao-Paulo?'),
        ):
            item = self.response(self.rows[0])
            item['a'], item['question'] = answer, stem
            with self.subTest(answer=answer), self.assertRaisesRegex(ValueError, 'leaks'):
                g.validate(item, self.rows[0])
        item = self.response(self.rows[0])
        item['a'], item['question'] = 'US', 'Which house was completed in 1900?'
        g.validate(item, self.rows[0])

    def test_reject_hidden_article_references(self):
        item = self.response(self.rows[0])
        item['question'] = 'Which city founded this company?'
        with self.assertRaisesRegex(ValueError, 'hidden article context'):
            g.validate(item, self.rows[0])

    def test_reject_leak_and_unsupported_evidence(self):
        item = self.response(self.rows[0])
        item['question'] = 'Was Article 1 founded in Paris?'
        with self.assertRaisesRegex(ValueError, 'leak'):
            g.validate(item, self.rows[0])
        item['question'] = 'In which city was Article 1 founded?'
        item['metadata']['evidence'] = 'unsupported'
        with self.assertRaisesRegex(ValueError, 'evidence'):
            g.validate(item, self.rows[0])


if __name__ == '__main__':
    unittest.main()
