import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import generate_targeted_trivia as target


class TargetedGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(Path(self.tmp.name) / 'trivia.sqlite')
        self.addCleanup(self.conn.close)
        target.create_schema(self.conn)
        self.article = dict(page_id='1', title='Coffee', description='A brewed drink',
                            abstract='Coffee is a brewed drink prepared from roasted coffee beans.',
                            source_revision_id='11', url='https://example.test/coffee', rank='1')

    def response(self):
        return dict(category='food_drink', subcategory='Beverages', level=2,
                    question='Which brewed beverage is prepared from roasted beans?',
                    a='Coffee', b='Tea', c='Milk', d='Juice',
                    metadata=dict(fact_tested='Coffee is prepared from roasted coffee beans.',
                                  evidence='Coffee is a brewed drink prepared from roasted coffee beans.',
                                  explanation='The abstract identifies coffee beans as the source.'), funfact=None)

    def artifact(self, *, artifact_id='artifact-1', raw=None):
        return dict(id=artifact_id, target_category='food_drink', article=self.article,
                    prompt='target prompt', raw_response=json.dumps(self.response()) if raw is None else raw,
                    usage=dict(input=2, output=3), error=None)

    def test_parse_categories_accepts_all_non_film_categories(self):
        categories = ('food_drink','games_hobbies','arts_design','business_economics','people_society',
                      'science_nature','religion_philosophy','literature_language','politics_law','technology',
                      'geography','sports','music','history')
        self.assertEqual(target.parse_categories(','.join(categories)), categories)

    def test_parse_categories_rejects_unknown_and_empty_ids(self):
        self.assertEqual(target.parse_categories('business_economics,people_society,literature_language,technology'),
                         ('business_economics', 'people_society', 'literature_language', 'technology'))
        self.assertEqual(target.parse_categories('food_drink,games_hobbies'), ('food_drink', 'games_hobbies'))
        for value in ('', 'food_drink,not_a_category'):
            with self.subTest(value=value), self.assertRaisesRegex(Exception, 'categories'):
                target.parse_categories(value)

    def test_hints_filter_plausible_sources(self):
        self.assertTrue(target.matches_category(self.article, 'food_drink'))
        self.assertTrue(target.matches_category(dict(self.article, title='Ada Lovelace', description='Computer programmer', abstract=''), 'technology'))
        self.assertTrue(target.matches_category(dict(self.article, title='Pride and Prejudice', description='Novel by Jane Austen', abstract=''), 'literature_language'))
        self.assertFalse(target.matches_category(dict(self.article, title='United Nations', description='International organization', abstract='An international organization.'), 'food_drink'))

    def test_targeted_no_match_does_not_consume_article_job(self):
        spool = Path(self.tmp.name) / 'spool'
        target.write_spool_result(spool, self.artifact(raw='{"skip_reason":"No food question"}'))
        self.assertEqual(target.ingest_spool(self.conn, spool), (0, 1, 0))
        self.assertEqual(self.conn.execute('select count(*) from article_jobs').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('select status from targeted_attempts').fetchone()[0], 'no_match')

    def test_ambiguous_review_is_no_match_and_keeps_page_available(self):
        artifact = self.artifact()
        artifact['quality_review'] = dict(accepted=False, matching_choices=['A', 'B'],
                                          reason='Both choices fit the stem.', raw_response='review')
        spool = Path(self.tmp.name) / 'spool'
        target.write_spool_result(spool, artifact)
        self.assertEqual(target.ingest_spool(self.conn, spool), (0, 1, 0))
        self.assertEqual(self.conn.execute('select count(*) from questions').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('select count(*) from article_jobs').fetchone()[0], 0)
        row = self.conn.execute('select status,quality_review from targeted_attempts').fetchone()
        self.assertEqual(row[0], 'no_match')
        self.assertIn('Both choices fit', row[1])

    def test_category_mismatch_is_no_match_and_keeps_page_available(self):
        item = self.response()
        item['category'] = 'music'
        item['subcategory'] = 'Music'
        spool = Path(self.tmp.name) / 'spool'
        target.write_spool_result(spool, self.artifact(raw=json.dumps(item)))
        self.assertEqual(target.ingest_spool(self.conn, spool), (0, 1, 0))
        self.assertEqual(self.conn.execute('select count(*) from questions').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('select count(*) from article_jobs').fetchone()[0], 0)
        row = self.conn.execute('select status,error from targeted_attempts').fetchone()
        self.assertEqual(row[0], 'no_match')
        self.assertIn('music', row[1])

    def test_valid_targeted_result_creates_one_normal_question(self):
        spool = Path(self.tmp.name) / 'spool'
        target.write_spool_result(spool, self.artifact())
        self.assertEqual(target.ingest_spool(self.conn, spool), (1, 0, 0))
        self.assertEqual(self.conn.execute('select status from article_jobs').fetchone()[0], 'done')
        self.assertEqual(self.conn.execute('select category from questions').fetchone()[0], 'food_drink')
        self.assertEqual(self.conn.execute('select status from targeted_attempts').fetchone()[0], 'done')

    def test_targeted_spool_import_is_idempotent(self):
        spool = Path(self.tmp.name) / 'spool'
        artifact = self.artifact()
        target.write_spool_result(spool, artifact)
        target.ingest_spool(self.conn, spool)
        target.write_spool_result(spool, artifact)
        self.assertEqual(target.ingest_spool(self.conn, spool), (0, 0, 0))
        self.assertEqual(self.conn.execute('select count(*) from questions').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('select count(*) from targeted_attempts').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
