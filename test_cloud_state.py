import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import cloud_state
import collect


class CloudStateTests(unittest.TestCase):
    def test_long_post_is_not_published_and_hashed_alias_survives_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = collect.init_db(root / 'original.sqlite3')
            item = dict(url='https://example.test/long-post', title='Privacy ' + 'long discussion ' * 45,
                        excerpt='Raw excerpt', published=collect.utcnow(), topics=['privacy'],
                        source_id='one', source_label='Source', region='world',
                        source_kind='commentary', first_seen=collect.iso(collect.utcnow()))
            collect.merge_items(db, [item]); db.close()
            cloud_state.export(root / 'original.sqlite3', root / 'state')
            published = json.loads((root / 'state/items.jsonl').read_text())
            self.assertLessEqual(len(published['title']), 181)
            self.assertNotIn(item['title'], (root / 'state/items.jsonl').read_text())
            cloud_state.restore(root / 'state', root / 'data')
            db = collect.init_db(root / 'data/archive.sqlite3')
            collect.merge_items(db, [{**item, 'url': 'https://mirror.test/repost'}])
            self.assertEqual(len(collect.read_recent(db, 30)), 1)
            db.close()

    def test_round_trip_keeps_provenance_first_seen_and_dedup_without_excerpts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = collect.init_db(root / 'original.sqlite3')
            stamp = collect.iso(collect.utcnow())
            item = dict(url='https://example.test/news?utm_source=rss', title='Privacy decision',
                        excerpt='LONG EXCERPT MUST NOT BE PUBLISHED', published=collect.utcnow(),
                        topics=['privacy'], source_id='one', source_label='Regulator', region='world',
                        source_kind='primary', first_seen=stamp)
            collect.merge_items(db, [item]); db.close()
            self.assertEqual(cloud_state.export(root / 'original.sqlite3', root / 'state'), 1)
            text = (root / 'state/items.jsonl').read_text()
            self.assertNotIn('EXCERPT', text)
            cloud_state.restore(root / 'state', root / 'data')
            db = collect.init_db(root / 'data/archive.sqlite3')
            rows = collect.read_recent(db, 30)
            self.assertEqual(rows[0]['first_seen_utc'], stamp)
            self.assertEqual(rows[0]['source_ids'], ['one'])
            collect.merge_items(db, [{**item, 'url': 'https://mirror.test/same'}])
            self.assertEqual(len(collect.read_recent(db, 30)), 1)
            db.close()
            cloud_state.export(root / 'data/archive.sqlite3', root / 'second-state')
            self.assertNotIn('EXCERPT', (root / 'second-state/items.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
