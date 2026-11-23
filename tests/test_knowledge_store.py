import tempfile
import shutil
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parents[1] / 'runtime'))
from knowledge_store import KnowledgeStore

class KnowledgeStoreTests(unittest.TestCase):
    def test_authorize_and_revoke(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            store = KnowledgeStore(Path(folder) / 'zero.db')
            root_id = store.authorize_root(folder)
            store.authorize_root(folder)
            self.assertEqual(len(store.list_roots()), 1)
            store.revoke_root(root_id)
            self.assertEqual(store.list_roots()[0]['enabled'], 0)

    def test_source_chunks_cascade(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
            store = KnowledgeStore(Path(folder) / 'zero.db')
            source_id = store.upsert_source('local', 'x.txt', 'x', '', 1, 'hash')
            store.replace_chunks(source_id, ['one', 'two'])
            store.delete_source(source_id)
            conn = store.connect()
            try:
                self.assertEqual(conn.execute('SELECT count(*) FROM chunks').fetchone()[0], 0)
            finally:
                conn.close()

if __name__ == '__main__': unittest.main()
