import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class KnowledgeStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self):
        with self.connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS authorized_roots (
                id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL,
                granted_at TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, uri TEXT UNIQUE NOT NULL,
                title TEXT, modified_at TEXT, indexed_at TEXT NOT NULL,
                size_bytes INTEGER, content_hash TEXT, is_private INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                chunk_index INTEGER NOT NULL, chunk_text TEXT NOT NULL, embedding BLOB, trust_score REAL DEFAULT 0.5,
                UNIQUE(source_id, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS search_cache (
                query_hash TEXT PRIMARY KEY, sanitized_query TEXT NOT NULL, results_json TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(title, chunk_text, content='chunks', content_rowid='id');
            """)

    def authorize_root(self, path):
        normalized = str(Path(path).expanduser().resolve())
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM authorized_roots WHERE path=?", (normalized,)).fetchone()
            if row:
                conn.execute("UPDATE authorized_roots SET enabled=1 WHERE id=?", (row['id'],))
                return row['id']
            cur = conn.execute("INSERT INTO authorized_roots(path, granted_at) VALUES (?,?)", (normalized, datetime.now(timezone.utc).isoformat()))
            return cur.lastrowid

    def list_roots(self):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM authorized_roots ORDER BY id")]

    def revoke_root(self, root_id):
        with self.connect() as conn:
            conn.execute("UPDATE authorized_roots SET enabled=0 WHERE id=?", (root_id,))

    def upsert_source(self, source_type, uri, title, modified_at, size_bytes, content_hash, is_private=1):
        with self.connect() as conn:
            conn.execute("""INSERT INTO sources(source_type,uri,title,modified_at,indexed_at,size_bytes,content_hash,is_private)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(uri) DO UPDATE SET title=excluded.title,modified_at=excluded.modified_at,indexed_at=excluded.indexed_at,size_bytes=excluded.size_bytes,content_hash=excluded.content_hash,is_private=excluded.is_private""",
                (source_type, uri, title, modified_at, datetime.now(timezone.utc).isoformat(), size_bytes, content_hash, is_private))
            return conn.execute("SELECT id FROM sources WHERE uri=?", (uri,)).fetchone()[0]

    def replace_chunks(self, source_id, chunks):
        with self.connect() as conn:
            conn.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))
            for index, item in enumerate(chunks):
                text, embedding = item if isinstance(item, tuple) else (item, None)
                conn.execute("INSERT INTO chunks(source_id,chunk_index,chunk_text,embedding) VALUES(?,?,?,?)", (source_id, index, text, embedding))

    def delete_source(self, source_id):
        with self.connect() as conn:
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))

    @staticmethod
    def hash_query(query):
        return hashlib.sha256(query.encode('utf-8')).hexdigest()
