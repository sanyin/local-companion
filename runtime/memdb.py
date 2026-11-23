"""记忆的存储层：SQLite，一条记忆一行。

为什么换掉 memories.json：
    那个文件是"全量读进内存、每次改动全量重写"。几百条没问题，五千条开始
    每聊一句都要重写几 MB；而真正要命的是**检索**——每次都要把所有条目
    过一遍字符串切割。规模上去以后那不是慢，是卡住。

保留不动的东西（这些是"0"能记得住人的原因，不能为了性能丢）：
    · 待确认才生效，用户否定过的永远不再提
    · 每条带出处（哪次会话、什么时候、原话）
    · supersedes 替换而不是叠加，旧条目标 superseded 但留档
    · journal.jsonl 是不可变账本，误删七天可恢复，整份可导出
    · 冷热只降温、不删除

新加的三样：
    · FTS5 索引（索引的是 2-gram，不是原文——见 _gram 的注释）
    · heat / use_count / confidence 三个可演化字段
    · 向量列（有就参与语义打分，没有就整条链路照跑）

对外接口跟老的 MemoryStore 同签名，所以 core 里换一行就能切过来。
"""

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memrank

LOCAL_TZ = timezone(timedelta(hours=8))
MAX_MEMORY_CHARS = 240
DEFAULT_PROMPT_LIMIT = memrank.DEFAULT_LIMIT

_KINDS = ("fact", "preference", "event", "style")
_STATUSES = ("pending", "active", "superseded", "quarantined", "archived")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  mem_rowid     INTEGER PRIMARY KEY,
  id            TEXT UNIQUE NOT NULL,
  text          TEXT NOT NULL,
  gram          TEXT NOT NULL DEFAULT '',
  kind          TEXT NOT NULL DEFAULT 'fact',
  source        TEXT NOT NULL DEFAULT 'user_explicit',
  status        TEXT NOT NULL DEFAULT 'active',
  importance    INTEGER NOT NULL DEFAULT 3,
  confidence    REAL NOT NULL DEFAULT 1.0,
  pinned        INTEGER NOT NULL DEFAULT 0,
  heat          REAL NOT NULL DEFAULT 1.0,
  created_at    TEXT NOT NULL,
  valid_from    TEXT,
  valid_to      TEXT,
  superseded_by TEXT,
  last_used     TEXT,
  use_count     INTEGER NOT NULL DEFAULT 0,
  evidence      TEXT,
  approved_at   TEXT,
  meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_kind   ON memories(kind);
CREATE INDEX IF NOT EXISTS idx_mem_heat   ON memories(heat);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  gram, content='memories', content_rowid='mem_rowid', tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS embeddings (
  memory_id  TEXT PRIMARY KEY,
  model_id   TEXT NOT NULL,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS retrieval_trace (
  id   INTEGER PRIMARY KEY,
  ts   TEXT NOT NULL,
  query TEXT NOT NULL,
  picked TEXT NOT NULL,
  tokens INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS quarantine (
  id         TEXT PRIMARY KEY,
  text       TEXT NOT NULL,
  kind       TEXT,
  reason     TEXT NOT NULL,
  near_id    TEXT,
  similarity REAL,
  created_at TEXT NOT NULL
);
"""


def _now():
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def _new_id(prefix="mem"):
    import uuid
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _gram(text):
    """索引用的 2-gram 串。

    为什么不直接索引原文：FTS5 的 unicode61 会把连续汉字当成**一个** token，
    「我住在上海」是一个词，搜「上海」永远匹配不到。也不用 trigram 分词器，
    它要求查询至少 3 个字符。所以自己切 2-gram、空格分开、按词索引。
    """
    return " ".join(sorted(memrank.grams(text)))


@contextmanager
def _session(db_path):
    """一个事务一个连接。退出必关——不关的话 Windows 删不掉 WAL 文件。"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _encode(vec):
    import struct
    return struct.pack(f"<{len(vec)}f", *[float(x) for x in vec])


def _decode(blob, dim):
    import struct
    if not blob or int(dim or 0) <= 0:
        return None
    return list(struct.unpack(f"<{dim}f", blob))


class MemDB:
    """一条记忆一行。对外方法名跟 MemoryStore 保持一致。"""

    def __init__(self, memory_file, db_path=None, embedder=None):
        self.memory_file = Path(memory_file)
        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(db_path) if db_path else self.memory_file.with_name("zero.db")
        self.rejected_path = self.memory_file.with_name("rejected.json")
        self.prefs_path = self.memory_file.with_name("preferences.jsonl")
        self.journal_path = self.memory_file.with_name("journal.jsonl")
        self.embedder = embedder          # callable(text) -> list[float] | None
        self._lock = threading.RLock()
        self._gram_cache = {}             # id -> (text, 2-gram 集合)，免得每轮重切
        self._vec_count = None
        self._init_db()

    # ---------- 连接与建表 ----------

    def _connect(self):
        """一个事务一个连接，退出时关掉。Windows 上不关会被 WAL 锁住文件。"""
        return _session(self.db_path)

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript(SCHEMA)
            row = conn.execute("SELECT value FROM index_meta WHERE key='migrated_at'").fetchone()
            empty = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"] == 0
            if row is None and empty:
                imported = self._migrate_from_json(conn)
                conn.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES('migrated_at',?)",
                             (_now(),))
                if imported:
                    conn.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES('imported',?)",
                                 (str(imported),))

    def _migrate_from_json(self, conn):
        """把老的 memories.json 搬进库。搬完不改原文件——留作对照和回滚。"""
        try:
            raw = json.loads(self.memory_file.read_text(encoding="utf-8") or "[]")
        except (FileNotFoundError, json.JSONDecodeError):
            return 0
        if isinstance(raw, dict):
            raw = raw.get("memories") or []
        count = 0
        for item in raw:
            if not isinstance(item, dict) or not (item.get("text") or "").strip():
                continue
            text = " ".join(str(item["text"]).split())[:MAX_MEMORY_CHARS]
            stamp = item.get("created_at") or _now()
            row = {
                "id": item.get("id") or _new_id(),
                "text": text,
                "kind": item.get("kind") if item.get("kind") in _KINDS else "fact",
                "source": item.get("source") or "user_explicit",
                "status": item.get("status") if item.get("status") in _STATUSES else "active",
                "importance": int(item.get("importance") or 3),
                "confidence": 0.9 if item.get("explicit") else 0.6,
                "pinned": 1 if item.get("pinned") else 0,
                "heat": float(item.get("heat") or 1.0),
                "created_at": stamp,
                "valid_from": item.get("valid_from") or stamp,
                "valid_to": item.get("valid_to"),
                "superseded_by": item.get("superseded_by"),
                "last_used": item.get("last_used"),
                "use_count": int(item.get("use_count") or 0),
                "evidence": item.get("evidence"),
                "approved_at": item.get("approved_at"),
                "meta": {"supersedes": item.get("supersedes") or []},
            }
            self._insert(conn, row)
            count += 1
        return count

    # ---------- 行读写 ----------

    def _insert(self, conn, row):
        conn.execute(
            """INSERT INTO memories
               (id,text,gram,kind,source,status,importance,confidence,pinned,heat,
                created_at,valid_from,valid_to,superseded_by,last_used,use_count,evidence,approved_at,meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["text"], _gram(row["text"]), row["kind"], row["source"],
             row["status"], row["importance"], row["confidence"], row["pinned"], row["heat"],
             row["created_at"], row.get("valid_from"), row.get("valid_to"),
             row.get("superseded_by"), row.get("last_used"), row.get("use_count", 0),
             json.dumps(row["evidence"], ensure_ascii=False) if row.get("evidence") else None,
             row.get("approved_at"), json.dumps(row.get("meta") or {}, ensure_ascii=False)),
        )
        mem_rowid = conn.execute("SELECT last_insert_rowid() AS r").fetchone()["r"]
        conn.execute("INSERT INTO memories_fts(rowid, gram) VALUES (?,?)", (mem_rowid, _gram(row["text"])))
        return mem_rowid

    def _to_dict(self, row, vecs=None):
        item = {
            "id": row["id"],
            "text": row["text"],
            "kind": row["kind"],
            "source": row["source"],
            "status": row["status"],
            "importance": row["importance"],
            "confidence": row["confidence"],
            "pinned": bool(row["pinned"]),
            "heat": row["heat"],
            "created_at": row["created_at"],
            "valid_from": row["valid_from"],
            "valid_to": row["valid_to"],
            "superseded_by": row["superseded_by"],
            "last_used": row["last_used"],
            "use_count": row["use_count"],
            "evidence": json.loads(row["evidence"]) if row["evidence"] else None,
            "approved_at": row["approved_at"],
            "explicit": row["source"] == "user_explicit",
        }
        raw_meta = row["meta"]
        meta = json.loads(raw_meta) if raw_meta and raw_meta != "{}" else {}
        item["supersedes"] = meta.get("supersedes") or []
        if meta:
            item["meta"] = meta
        if vecs is not None:
            hit = vecs.get(row["id"])
            if hit:
                item["vec"] = hit
        return item

    def _vecs(self, ids):
        if not ids:
            return {}
        if self._vec_count is None:
            with self._connect() as conn:
                self._vec_count = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
        if not self._vec_count:
            return {}                     # 没建过向量就别去查表，省一次全表 IN
        marks = ",".join("?" for _ in ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT memory_id, vec, dim FROM embeddings WHERE memory_id IN ({marks})", list(ids)
            ).fetchall()
        return {row["memory_id"]: _decode(row["vec"], row["dim"]) for row in rows}

    def _rows(self, where="1=1", params=(), order="created_at DESC, mem_rowid DESC", with_vec=False):
        with self._connect() as conn:
            rows = conn.execute(f"SELECT * FROM memories WHERE {where} ORDER BY {order}", params).fetchall()
        vecs = self._vecs([row["id"] for row in rows]) if with_vec else None
        return [self._to_dict(row, vecs) for row in rows]

    def _annotate(self, rows):
        """把 2-gram 挂在行上，并缓存。规模上去以后这一步是热点。"""
        for row in rows:
            cached = self._gram_cache.get(row["id"])
            if cached and cached[0] == row["text"]:
                row["_grams"] = cached[1]
            else:
                body = memrank.grams(row["text"])
                self._gram_cache[row["id"]] = (row["text"], body)
                row["_grams"] = body
        return rows

    def _fts_ids(self, text, limit=200):
        """用 2-gram 索引进候选，别把整个库都算一遍。

        查询侧也要切 2-gram 再 OR 起来——索引里存的就是 2-gram 词。
        一个词都不命中就返回空，让调用方退回"最近的几条"。
        """
        parts = [f'"{g}"' for g in sorted(memrank.grams(text)) if '"' not in g]
        if not parts:
            return []
        query = " OR ".join(parts)
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT m.id AS id FROM memories_fts f JOIN memories m ON m.mem_rowid=f.rowid "
                    "WHERE memories_fts MATCH ? ORDER BY rank LIMIT ?", (query, int(limit))).fetchall()
        except sqlite3.Error:
            return []
        return [row["id"] for row in rows]

    def _candidates(self, text, limit=200, recent=40):
        """候选池：置顶 ∪ 索引命中 ∪ 最近若干。

        小库直接全取；大库只在 SQL 里挑出这几拨再取行——不能为了挑 12 条
        先把三千条全读进内存，那就是把老实现的毛病换了个地方犯。

        索引没命中（比如全新话题）就退回最近的，不至于一条都挑不出来。
        """
        total = self.count()
        if total <= 800 or not text:
            return self._annotate(self.active())

        pool_ids = [row["id"] for row in
                    self._rows("status='active' AND pinned=1")]
        pool_ids += self._fts_ids(text, limit=limit)
        with self._connect() as conn:
            recent_rows = conn.execute(
                "SELECT id FROM memories WHERE status='active' "
                "ORDER BY created_at DESC, mem_rowid DESC LIMIT ?", (int(recent),)).fetchall()
        pool_ids += [row["id"] for row in recent_rows]

        seen, unique = set(), []
        for memory_id in pool_ids:
            if memory_id not in seen:
                seen.add(memory_id)
                unique.append(memory_id)
        if not unique:
            return []
        marks = ",".join("?" for _ in unique)
        return self._annotate(self._rows(f"status='active' AND id IN ({marks})", unique))

    # ---------- 查询 ----------

    def all(self):
        return self._rows()

    def active(self, with_vec=False):
        return self._rows("status='active'", with_vec=with_vec)

    def pending(self):
        return self._rows("status='pending'")

    def superseded(self):
        return self._rows("status='superseded'")

    def find(self, memory_id):
        """按 id 取一条。不带向量——向量只在检索路径上要，进 journal/导出会变噪声。"""
        rows = self._rows("id=?", (memory_id,))
        return rows[0] if rows else None

    def count(self):
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]

    def similar_active(self, text, limit=5, with_vec=True):
        """跟这句话最像的已生效记忆。抽取时用来判断「是不是在更新旧事实」。

        字面相似不够（「我搬到北京了」和「主人住在上海」零重合），所以把
        向量也放进来；还不够就用最近的补齐——搬家换工作这类更新，
        旧事实通常就是最近记下的那条。
        """
        rows = [row for row in self._candidates(text, limit=300, recent=20)
                if row.get("kind") != "style"]
        if with_vec:
            vecs = self._vecs([row["id"] for row in rows])
            for row in rows:
                row["vec"] = vecs.get(row["id"])
        query_vec = self._embed(text) if (with_vec and self.embedder) else None
        query_grams = memrank.grams(text)
        scored = []
        for row in rows:
            detail = memrank.score_row(row, query_grams, query_vec=query_vec)
            scored.append((detail["sem"] + detail["lex"], row))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        picked = [row for value, row in scored if value > 0.12][:limit]
        if len(picked) < limit:
            seen = {row["id"] for row in picked}
            for row in sorted(rows, key=lambda item: item.get("created_at") or "", reverse=True):
                if row["id"] in seen:
                    continue
                picked.append(row)
                seen.add(row["id"])
                if len(picked) >= limit:
                    break
        return picked

    def contains_snippet(self, text, min_len=8):
        """这句话里有没有整段记忆。search_web 是唯一能不经确认把数据送出去的口子。"""
        haystack = memrank.grams(text)
        if not haystack:
            return None
        for row in self.active():
            if row.get("kind") == "style":
                continue
            body = "".join(ch for ch in row["text"].lower() if not ch.isspace())
            if len(body) < min_len:
                continue
            window = {body[i:i + min_len] for i in range(len(body) - min_len + 1)}
            probe = "".join(ch for ch in text.lower() if not ch.isspace())
            if any(part in probe for part in window):
                return row["text"]
        return None

    # ---------- 写入 ----------

    def add(self, text, kind="fact", source="user_explicit", status="active", pinned=False,
            explicit=True, importance=None, evidence=None, supersedes=None):
        text = " ".join((text or "").split()).strip()[:MAX_MEMORY_CHARS]
        if not text:
            return None
        if status not in _STATUSES:
            status = "active"
        if kind not in _KINDS:
            kind = "fact"
        rejected = self._load_rejected()

        with self._lock, self._connect() as conn:
            if source != "user_explicit" and text in rejected:
                return None
            exact = conn.execute("SELECT * FROM memories WHERE text=?", (text,)).fetchone()
            if exact is not None and exact["status"] != "superseded":
                if status == "active" and exact["status"] != "active":
                    conn.execute("UPDATE memories SET status='active', approved_at=? WHERE id=?",
                                 (_now(), exact["id"]))
                    self._journal("reactivate", self._to_dict(exact))
                return self._to_dict(conn.execute("SELECT * FROM memories WHERE id=?", (exact["id"],)).fetchone())
            if exact is not None and exact["status"] == "superseded" and status == "active":
                stamp = _now()
                conn.execute(
                    "UPDATE memories SET status='active', valid_from=?, valid_to=NULL, "
                    "superseded_by=NULL, approved_at=? WHERE id=?", (stamp, stamp, exact["id"]))
                self._journal("reactivate", self._to_dict(exact))
                return self._to_dict(conn.execute("SELECT * FROM memories WHERE id=?", (exact["id"],)).fetchone())

            stamp = _now()
            row = {
                "id": _new_id(), "text": text, "kind": kind, "source": source,
                "status": status, "importance": int(importance if importance is not None
                                                    else _default_importance(kind, source)),
                "confidence": 0.9 if explicit else 0.6,
                "pinned": 1 if pinned else 0, "heat": 1.0, "created_at": stamp,
                "valid_from": stamp, "valid_to": None, "superseded_by": None,
                "last_used": None, "use_count": 0, "evidence": evidence,
                "approved_at": _now() if status == "active" and source == "user_explicit" else None,
                "meta": {"supersedes": [str(i) for i in (supersedes or [])][:3]},
            }
            self._insert(conn, row)
            if self.embedder:
                self._store_vec(conn, row["id"], text)
        item = self.find(row["id"])
        self._journal("add", item)
        return item

    def confirm(self, memory_id):
        """用户点头。注意不把 source 改成 user_explicit——
        「她猜的、你批准了」和「你亲口说的」是两种东西，混成一种就没法审计。"""
        with self._lock, self._connect() as conn:
            target = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if target is None:
                return None
            was_pending = target["status"] == "pending"
            stamp = _now()
            conn.execute("UPDATE memories SET status='active', approved_at=?, valid_from=? WHERE id=?",
                         (stamp, stamp, memory_id))
            meta = json.loads(target["meta"] or "{}")
            superseded = []
            for old_id in meta.get("supersedes") or []:
                old = conn.execute("SELECT * FROM memories WHERE id=?", (old_id,)).fetchone()
                if old is None or old["id"] == memory_id or old["status"] != "active":
                    continue
                conn.execute(
                    "UPDATE memories SET status='superseded', valid_to=?, superseded_by=? WHERE id=?",
                    (stamp, memory_id, old_id))
                superseded.append(old_id)
        item = self.find(memory_id)
        self._journal("confirm", item, {"superseded": superseded})
        if was_pending:
            self._log_preference(item, "accept")
        return item

    def pin(self, memory_id, pinned=True):
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE memories SET pinned=? WHERE id=?", (1 if pinned else 0, memory_id))
        return self.find(memory_id)

    def forget(self, memory_id):
        """从库里删，但 journal 留快照——误点还有七天可恢复。"""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                return False
            item = self._to_dict(row)
            if item["status"] != "active":
                rejected = self._load_rejected()
                rejected.add(item["text"])
                self._save_rejected(rejected)
            conn.execute("DELETE FROM memories_fts WHERE rowid=?", (row["mem_rowid"],))
            conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            conn.execute("DELETE FROM embeddings WHERE memory_id=?", (memory_id,))
        self._journal("forget", item)
        if item["status"] == "pending":
            self._log_preference(item, "reject")
        return True

    def forgotten(self, days=7):
        current = {item["id"] for item in self.all()}
        cutoff = datetime.now(LOCAL_TZ) - timedelta(days=days)
        found = {}
        for record in reversed(self._journal_records()):
            if record.get("event") != "forget":
                continue
            item = record.get("item") or {}
            memory_id = item.get("id") or record.get("id")
            if not memory_id or memory_id in current or memory_id in found:
                continue
            moment = memrank._parse(record.get("ts"))
            if moment is None or moment < cutoff:
                continue
            found[memory_id] = item
        return list(found.values())

    def restore(self, memory_id):
        if self.find(memory_id) is not None:
            return None
        snapshot = next((item for item in self.forgotten() if item.get("id") == memory_id), None)
        if snapshot is None:
            return None
        restored = dict(snapshot)
        restored.update(valid_to=None, superseded_by=None)
        with self._lock, self._connect() as conn:
            self._insert(conn, {
                "id": restored["id"], "text": restored["text"],
                "kind": restored.get("kind") or "fact",
                "source": restored.get("source") or "user_explicit",
                "status": restored.get("status") or "pending",
                "importance": int(restored.get("importance") or 3),
                "confidence": float(restored.get("confidence") or 0.7),
                "pinned": 1 if restored.get("pinned") else 0,
                "heat": float(restored.get("heat") or 1.0),
                "created_at": restored.get("created_at") or _now(),
                "valid_from": restored.get("valid_from"), "valid_to": None,
                "superseded_by": None, "last_used": None,
                "use_count": int(restored.get("use_count") or 0),
                "evidence": restored.get("evidence"),
                "approved_at": restored.get("approved_at"),
                "meta": {"supersedes": restored.get("supersedes") or []},
            })
        if restored.get("status") == "pending":
            rejected = self._load_rejected()
            rejected.discard(restored.get("text") or "")
            self._save_rejected(rejected)
        item = self.find(memory_id)
        self._journal("restore", item)
        return item

    def touch(self, memory_id):
        self.touch_many([memory_id])

    def touch_many(self, memory_ids):
        wanted = [i for i in (memory_ids or []) if i]
        if not wanted:
            return
        stamp = _now()
        marks = ",".join("?" for _ in wanted)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE memories SET last_used=?, use_count=use_count+1, "
                f"heat=MIN(1.5, heat+0.15) WHERE id IN ({marks})", [stamp] + wanted)

    def set_heat(self, memory_id, value):
        with self._lock, self._connect() as conn:
            conn.execute("UPDATE memories SET heat=? WHERE id=?",
                         (max(0.0, min(1.5, float(value))), memory_id))

    def set_status(self, memory_id, status, meta_extra=None):
        if status not in _STATUSES:
            raise ValueError(f"不认识的 status：{status}")
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                return None
            raw_meta = row["meta"]
            meta = json.loads(raw_meta) if raw_meta and raw_meta != "{}" else {}
            if meta_extra:
                meta.update(meta_extra)
            conn.execute("UPDATE memories SET status=?, meta=? WHERE id=?",
                         (status, json.dumps(meta, ensure_ascii=False), memory_id))
        return self.find(memory_id)

    # ---------- 向量 ----------

    def _embed(self, text):
        if not self.embedder:
            return None
        try:
            return self.embedder(text)
        except Exception:
            return None

    def _store_vec(self, conn, memory_id, text):
        vec = self._embed(text)
        if not vec:
            return False
        conn.execute(
            "INSERT OR REPLACE INTO embeddings(memory_id,model_id,dim,vec,updated_at) "
            "VALUES (?,?,?,?,?)",
            (memory_id, getattr(self.embedder, "model_id", "local"), len(vec), _encode(vec), _now()))
        self._vec_count = None
        return True

    def index_embeddings(self, limit=None, progress=None):
        """补建向量索引。没装模型时返回 0，不报错。"""
        if not self.embedder:
            return 0
        todo = [row for row in self.active()
                if not row.get("vec")]
        if limit:
            todo = todo[:limit]
        done = 0
        with self._lock, self._connect() as conn:
            for row in todo:
                if self._store_vec(conn, row["id"], row["text"]):
                    done += 1
                if progress and done % 50 == 0:
                    progress(done, len(todo))
        return done

    # ---------- 进提示词 ----------

    def style_block(self, limit=8):
        rows = [row for row in self._rows("status='active' AND kind='style'")]
        if not rows:
            return ""
        rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return "\n".join(f"- {row['text']}" for row in rows[:limit])

    def prompt_block(self, text="", limit=DEFAULT_PROMPT_LIMIT, chars=memrank.DEFAULT_CHARS):
        """给模型看的记忆清单。挑法见 memrank：混合打分 + 去冗余 + 字符预算。"""
        rows = [row for row in self._candidates(text) if row.get("kind") != "style"]
        if not rows:
            return "（还没有记下什么。）"

        vecs = self._vecs([row["id"] for row in rows])
        if vecs:
            for row in rows:
                row["vec"] = vecs.get(row["id"])

        query_vec = self._embed(text) if (text and self.embedder) else None
        chosen, trace = memrank.select(rows, text, limit=limit, chars=chars, query_vec=query_vec)

        touched = [row["id"] for row in chosen
                   if row.get("pinned") or (row.get("_parts") or {}).get("lex", 0) > 0
                   or (row.get("_parts") or {}).get("sem", 0) > 0]
        self.touch_many(touched)
        self._record_trace(text, chosen, trace)

        lines = []
        for row in chosen:
            mark = "·" if row.get("source") == "user_explicit" else "·（我推断的，主人确认过）"
            lines.append(f"- {row['text']} {mark}")
        return "\n".join(lines)

    def _record_trace(self, query, chosen, trace):
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT INTO retrieval_trace(ts,query,picked,tokens) VALUES (?,?,?,?)",
                    (_now(), (query or "")[:200],
                     json.dumps([row["id"] for row in chosen], ensure_ascii=False),
                     sum(len(row.get("text") or "") for row in chosen)))
                conn.execute("DELETE FROM retrieval_trace WHERE id <= "
                             "(SELECT MAX(id)-500 FROM retrieval_trace)")
        except sqlite3.Error:
            pass

    # ---------- 导出 ----------

    def export_data(self):
        return {
            "exported_at": _now(),
            "memories": self.all(),
            "rejected": sorted(self._load_rejected()),
        }

    def sync_json(self, path=None):
        """把库写回一份 memories.json。降级回老版本、或者只想看纯文本时用。"""
        target = Path(path) if path else self.memory_file
        payload = json.dumps(self.all(), ensure_ascii=False, indent=2)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(target)
        return target

    def rebuild_index(self):
        with self._lock, self._connect() as conn:
            conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
        return True

    def stats(self):
        with self._connect() as conn:
            by_status = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) AS n FROM memories GROUP BY status")}
            vecs = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()["n"]
            edges = 0
            try:
                edges = conn.execute("SELECT COUNT(*) AS n FROM edges").fetchone()["n"]
            except sqlite3.Error:
                pass
        return {"total": sum(by_status.values()), "by_status": by_status,
                "vectors": vecs, "edges": edges, "db": str(self.db_path)}

    # ---------- 账本与偏好 ----------

    def _journal(self, event, item, extra=None):
        record = {"ts": _now(), "event": event, "id": (item or {}).get("id"), "item": dict(item or {})}
        if extra:
            record.update(extra)
        try:
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _journal_records(self):
        if not self.journal_path.exists():
            return []
        records = []
        for line in self.journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _load_rejected(self):
        try:
            raw = json.loads(self.rejected_path.read_text(encoding="utf-8") or "[]")
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        return {str(item) for item in raw if item} if isinstance(raw, list) else set()

    def _save_rejected(self, rejected):
        recent = sorted(rejected)[-200:]
        tmp = self.rejected_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.rejected_path)

    def _log_preference(self, item, decision):
        try:
            record = {
                "ts": _now(), "memory_id": item.get("id"), "decision": decision,
                "text": item.get("text"), "kind": item.get("kind"),
                "explicit": bool(item.get("explicit")), "source": item.get("source"),
                "importance": item.get("importance"),
                "supersedes": item.get("supersedes") or [],
                "has_evidence": bool(item.get("evidence")),
            }
            with self.prefs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _default_importance(kind, source):
    if kind == "preference":
        return 4
    if kind == "event":
        return 3
    return 4 if source == "user_explicit" else 3


def open_store(memory_file, db_path=None, embedder=None):
    """开关在配置里，但这里不读配置——调用方决定用哪个实现。"""
    return MemDB(memory_file, db_path=db_path, embedder=embedder)
