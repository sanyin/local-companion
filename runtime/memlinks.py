"""记忆之间的关系，和"让库自己变干净"的那一段。

两件事放一起，因为它们共用同一份相似度判断：

  1. 类型化边。不再是"每次把最近 60 条丢给模型重建整张图"，而是新记忆
     入库时只跟它的近邻连边。图只增、可修剪、不重建。每条边必须带
     evidence_ids 和 why——说不出"为什么连"的边，界面上就是一根噪声线。

  2. 退化闸（G3）。合并同义、降温久未用、归档冷区、把矛盾摆到台面上。
     这是老版本完全缺的一段：只降温不删除，所以敢收紧。

没有模型也能跑：规则边（同文、近义、时间相邻）先出，模型只用来复核
"这两条到底矛盾不矛盾"。装不上模型就退化成纯规则，不许抛异常。
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import memrank

LOCAL_TZ = timezone(timedelta(hours=8))

EDGE_TYPES = (
    "supersedes", "contradicts", "supports", "same_as",
    "instance_of", "part_of", "caused_by", "before_after", "analogous",
)

# 判定的门槛。都放在这里，别散在函数里。
SAME_AS = 0.90        # 相似到这个程度：说的大概是同一件事
NEAR_DUP = 0.92       # 更严一档：可以直接合并
ANALOGOUS_MIN = 0.30  # 类比的下限
ANALOGOUS_MAX = 0.62  # 再像就不是"类比"，是"同一件事"了
BEFORE_AFTER_DAYS = 45

HEAT_DAILY = 0.97
COLD = 0.30
COLD_DAYS = 90
ARCHIVE_DAYS = 365

SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
  id           TEXT PRIMARY KEY,
  src          TEXT NOT NULL,
  dst          TEXT NOT NULL,
  type         TEXT NOT NULL,
  weight       REAL NOT NULL DEFAULT 0.5,
  created_by   TEXT NOT NULL DEFAULT 'rule',
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  why          TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  UNIQUE(src, dst, type)
);
CREATE INDEX IF NOT EXISTS idx_edge_src ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edge_dst ON edges(dst);
"""


def _now():
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path):
    """一个事务一个连接，退出必关。Windows 上不关会锁住 WAL 文件。"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class EdgeStore:
    def __init__(self, db_path):
        self.db_path = db_path
        with _connect(db_path) as conn:
            conn.executescript(SCHEMA)

    def add(self, src, dst, kind, weight=0.5, created_by="rule", why="", evidence=None):
        if kind not in EDGE_TYPES or src == dst:
            return None
        edge_id = f"edge_{uuid.uuid4().hex[:10]}"
        with _connect(self.db_path) as conn:
            try:
                conn.execute(
                    "INSERT INTO edges(id,src,dst,type,weight,created_by,evidence_ids,why,created_at,status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'active')",
                    (edge_id, src, dst, kind, float(weight), created_by,
                     json.dumps(evidence or [src, dst], ensure_ascii=False),
                     why or "", _now()))
            except sqlite3.IntegrityError:
                return None
        return self.get(edge_id)

    def get(self, edge_id):
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM edges WHERE id=?", (edge_id,)).fetchone()
        return _edge_dict(row) if row else None

    def for_memory(self, memory_id, limit=12):
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM edges WHERE status='active' AND (src=? OR dst=?) "
                "ORDER BY weight DESC LIMIT ?", (memory_id, memory_id, limit)).fetchall()
        return [_edge_dict(row) for row in rows]

    def neighbors(self, memory_id, depth=2, limit=80):
        """局部邻域：从一条记忆出发走两跳。可视化里的"点开看看周围"。"""
        seen = {memory_id}
        frontier = [memory_id]
        edges = []
        for _ in range(max(1, depth)):
            next_frontier = []
            for node in frontier:
                for edge in self.for_memory(node, limit=limit):
                    key = (edge["src"], edge["dst"], edge["type"])
                    if edge["src"] in seen and edge["dst"] in seen:
                        continue
                    edges.append(edge)
                    for end in (edge["src"], edge["dst"]):
                        if end not in seen and len(seen) < limit:
                            seen.add(end)
                            next_frontier.append(end)
            frontier = next_frontier
            if not frontier:
                break
        return {"ids": sorted(seen), "edges": edges}

    def drop_for(self, memory_id):
        """一条记忆没了，指向它的边必须一起消失，否则图上会飘着指向虚空的线。"""
        with _connect(self.db_path) as conn:
            cur = conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (memory_id, memory_id))
            return cur.rowcount

    def count(self, kind=None):
        with _connect(self.db_path) as conn:
            if kind:
                return conn.execute("SELECT COUNT(*) AS n FROM edges WHERE type=? AND status='active'",
                                    (kind,)).fetchone()["n"]
            return conn.execute("SELECT COUNT(*) AS n FROM edges WHERE status='active'").fetchone()["n"]

    def prune_dangling(self, valid_ids):
        """清掉两端不全在库里的边。归档/删除之后跑一次。"""
        valid = set(valid_ids)
        removed = 0
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT id,src,dst FROM edges").fetchall()
            for row in rows:
                if row["src"] not in valid or row["dst"] not in valid:
                    conn.execute("DELETE FROM edges WHERE id=?", (row["id"],))
                    removed += 1
        return removed


def _edge_dict(row):
    return {
        "id": row["id"], "src": row["src"], "dst": row["dst"], "type": row["type"],
        "weight": row["weight"], "created_by": row["created_by"],
        "evidence_ids": json.loads(row["evidence_ids"] or "[]"),
        "why": row["why"], "created_at": row["created_at"], "status": row["status"],
    }


# ---------- 规则边 ----------

def similar(a, b):
    """两条记忆的相似度。有向量用向量，没有就字面。"""
    if a.get("vec") and b.get("vec"):
        value = memrank.cosine(a["vec"], b["vec"])
        if value > 0:
            return value
    return memrank.jaccard(memrank.grams(a.get("text")), memrank.grams(b.get("text")))


def rule_edges(item, candidates, now=None):
    """只产出代码能自证的那些边。判不了的留给模型，判不了又不确定就不连。"""
    now = now or datetime.now(LOCAL_TZ)
    found = []
    item_at = memrank._parse(item.get("created_at"))

    for other in candidates:
        if other.get("id") == item.get("id"):
            continue
        sim = similar(item, other)
        if sim >= SAME_AS:
            found.append({
                "src": other["id"], "dst": item["id"], "type": "same_as",
                "weight": round(sim, 3),
                "why": f"两句话重合度 {sim:.0%}，说的可能是同一件事",
            })
            continue

        other_at = memrank._parse(other.get("created_at"))
        same_kind = other.get("kind") == item.get("kind")
        if item_at and other_at and same_kind:
            days = abs((item_at - other_at).total_seconds()) / 86400
            if ANALOGOUS_MIN <= sim < ANALOGOUS_MAX and days <= BEFORE_AFTER_DAYS:
                kind = "before_after"
                why = f"同属「{item.get('kind')}」，相隔 {days:.0f} 天，像是同一件事的前后"
            elif ANALOGOUS_MIN <= sim < ANALOGOUS_MAX:
                kind = "analogous"
                why = f"结构相似但隔了 {days:.0f} 天，像是同类处境"
            else:
                continue
            found.append({"src": other["id"], "dst": item["id"], "type": kind,
                          "weight": round(sim, 3), "why": why})

    # supersedes 是已经确定的替换关系，直接落成边
    for old_id in item.get("supersedes") or []:
        found.append({
            "src": old_id, "dst": item["id"], "type": "supersedes",
            "weight": 1.0, "why": "这条记忆声明了要替换上一条",
        })

    return found


def link_new(store, db, item, candidates=None, extractor=None, limit=5):
    """新记忆入库时连边。同步、有上限、不调全量。"""
    if item is None:
        return []
    pool = candidates if candidates is not None else db.similar_active(item["text"], limit=limit)
    pool = [row for row in pool if row.get("id") != item.get("id")][:max(1, limit)]
    edges = rule_edges(item, pool)

    existing = {(edge["src"], edge["dst"], edge["type"]) for edge in edges}
    for cand in pool:
        key = (item["id"], cand["id"], "same_as")
        if key in existing:
            continue
        pair = judge_contradiction(extractor, item, cand)
        if pair:
            edges.append(pair)

    saved = []
    for edge in edges:
        record = store.add(edge["src"], edge["dst"], edge["type"], weight=edge["weight"],
                           created_by=edge.get("created_by", "rule"), why=edge["why"])
        if record:
            saved.append(record)
    return saved


def judge_contradiction(extractor, a, b):
    """让模型只回答一个问题：这两条互相矛盾吗。

    矛盾是最值钱的边（"你三周前说每天跑步，昨天说膝盖疼"），
    但也是最容易误报的，所以宁可漏，不可乱连。
    """
    if extractor is None:
        return None
    prompt = (
        "下面两句话，是不是在说同一件事、而且互相矛盾？\n"
        "只输出 JSON：{\"contradicts\": true/false, \"reason\": \"一句话\"}\n"
        "不确定就输出 false——误连比漏连坏得多。\n\n"
        f"A：{a.get('text')}\nB：{b.get('text')}\n"
    )
    try:
        raw = extractor(prompt) or ""
    except Exception:
        return None
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not payload.get("contradicts"):
        return None
    return {
        "src": a["id"], "dst": b["id"], "type": "contradicts",
        "weight": 0.7, "created_by": "model",
        "why": str(payload.get("reason") or "两条记忆互相矛盾")[:120],
    }


# ---------- 退化闸 ----------

def _meta(row):
    return row.get("meta") or {}


def decay_heat(db, days=None, now=None):
    """过一天降一点温。只降温，不删除——这是敢收紧的前提。"""
    now = now or datetime.now(LOCAL_TZ)
    with _connect(db.db_path) as conn:
        row = conn.execute("SELECT value FROM index_meta WHERE key='heat_at'").fetchone()
        last = memrank._parse(row["value"]) if row else None
        if last is None or days is not None:
            elapsed = float(days if days is not None else 1.0)
        else:
            elapsed = max(0.0, (now - last).total_seconds() / 86400)
        if elapsed <= 0:
            return 0
        factor = HEAT_DAILY ** elapsed
        conn.execute("UPDATE memories SET heat=MAX(0.0, MIN(1.5, heat*?)) WHERE pinned=0", (factor,))
        conn.execute("INSERT OR REPLACE INTO index_meta(key,value) VALUES('heat_at',?)",
                     (now.isoformat(timespec="seconds"),))
    return round(factor, 4)


def merge_duplicates(db, store=None, threshold=NEAR_DUP, limit=50):
    """把说同一件事的两条并成一条：留重要的，另一条归档但留痕。

    绝不真删——导出里还要能看到"它曾经是两条"。
    """
    merged = []
    rows = db.active()
    for left, right in _pairs(rows, threshold, limit):
        left, right = db.find(left["id"]), db.find(right["id"])
        if not left or not right:
            continue
        if left.get("status") != "active" or right.get("status") != "active":
            continue                    # 刚刚已经被并走了，别二次合并
        keep, drop = _pick_keeper(left, right)
        db.set_status(drop["id"], "archived", {"merged_into": keep["id"]})
        if store is not None:
            store.add(keep["id"], drop["id"], "same_as", weight=0.95, why="合并重复")
        merged.append({"kept": keep["id"], "dropped": drop["id"]})
    return merged


def _pairs(rows, threshold, limit):
    found = []
    for i, left in enumerate(rows):
        for right in rows[i + 1:]:
            if similar(left, right) >= threshold:
                found.append((left, right))
                if len(found) >= limit:
                    return found
    return found


def _pick_keeper(a, b):
    """留哪条：置顶 > 重要 > 用的次数多 > 记得早。"""
    def key(item):
        return (bool(item.get("pinned")), int(item.get("importance") or 3),
                int(item.get("use_count") or 0), item.get("created_at") or "")
    return (a, b) if key(a) >= key(b) else (b, a)


def archive_cold(db, now=None, cold_days=COLD_DAYS, archive_days=ARCHIVE_DAYS):
    """冷区归档。仍然可导出、可显式搜到，只是不主动进上下文。"""
    now = now or datetime.now(LOCAL_TZ)
    archived = []
    for row in db.active():
        if row.get("pinned"):
            continue
        heat = float(row.get("heat") or 1.0)
        last_used = memrank._parse(row.get("last_used"))
        age_days = ((now - last_used).total_seconds() / 86400) if last_used else 10 ** 6
        if heat < COLD and age_days >= cold_days and age_days >= archive_days:
            db.set_status(row["id"], "archived", {"archived_reason": "long_cold"})
            archived.append(row["id"])
    return archived


def conflicts(db, store):
    """两条都还生效、却被判为矛盾的——摆到台面上，让用户裁决。

    不自动解决。自动"以新的为准"在事实层面可能对，在人的层面是粗暴的。
    """
    found = []
    with _connect(db.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM edges WHERE type='contradicts' AND status='active'").fetchall()
    for row in rows:
        left, right = db.find(row["src"]), db.find(row["dst"])
        if not left or not right:
            continue
        if left.get("status") == "active" and right.get("status") == "active":
            found.append({
                "edge_id": row["id"], "why": row["why"],
                "left": {"id": left["id"], "text": left["text"]},
                "right": {"id": right["id"], "text": right["text"]},
            })
    return found


def consolidate(db, store=None, now=None):
    """每日跑一次的那一段：降温 → 合并 → 归档 → 报矛盾。

    顺序有讲究：先降温，合并时的"留哪条"才看得到冷热；先合并再归档，
    否则刚合并出来的 archived 条目会被再归档一次。
    """
    now = now or datetime.now(LOCAL_TZ)
    if store is None:
        store = EdgeStore(db.db_path)
    factor = decay_heat(db, now=now)
    merged = merge_duplicates(db, store)
    archived = archive_cold(db, now=now)
    store.prune_dangling({row["id"] for row in db.all()})
    return {
        "at": _now(), "heat_factor": factor,
        "merged": merged, "archived": archived,
        "conflicts": conflicts(db, store),
        "edges": store.count(),
    }
