"""目标：让「要达到什么状态」变成一个对象，而不是一条提醒。

提醒是「11 月 25 日 09:00 通知我」；目标是「11 月 25 日前把材料交掉，
判据是 docs/报告.md 存在且超过 2KB」。

这个区别决定了系统能不能形成闭环：提醒响了就结束了，目标需要反复核对
「到了没有」。没有判据，就没法核对；没法核对，就只剩下一遍遍问你——
那是骚扰，不是能动性。

判据三种，按可观测性排序：
    file_exists / file_count   机器可判，最硬
    memory_matches             靠记忆里有没有相关进展
    checkin                    只能问本人，最软，也最容易变成骚扰
规则：能写成前两种的，绝不写成 checkin。
"""

import json
import sqlite3
from contextlib import contextmanager
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memrank

LOCAL_TZ = timezone(timedelta(hours=8))
OWNERS = ("user", "self")
SOURCES = ("user_explicit", "memory_inferred", "maintenance")
STATUSES = ("active", "stalled", "done", "abandoned", "expired")
MAX_SELF_GOALS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  owner         TEXT NOT NULL DEFAULT 'user',
  source        TEXT NOT NULL DEFAULT 'user_explicit',
  origin_ids    TEXT NOT NULL DEFAULT '[]',
  success       TEXT NOT NULL DEFAULT '{}',
  deadline      TEXT,
  cadence_days  INTEGER NOT NULL DEFAULT 7,
  importance    INTEGER NOT NULL DEFAULT 3,
  status        TEXT NOT NULL DEFAULT 'active',
  created_at    TEXT NOT NULL,
  last_progress_at TEXT,
  progress      REAL NOT NULL DEFAULT 0,
  progress_conf REAL NOT NULL DEFAULT 0.3,
  muted_until   TEXT,
  meta          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_goal_status ON goals(status);
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


def _row_to_goal(row):
    goal = {
        "id": row["id"], "title": row["title"], "owner": row["owner"], "source": row["source"],
        "origin_ids": json.loads(row["origin_ids"] or "[]"),
        "success": json.loads(row["success"] or "{}"),
        "deadline": row["deadline"], "cadence_days": row["cadence_days"],
        "importance": row["importance"], "status": row["status"],
        "created_at": row["created_at"], "last_progress_at": row["last_progress_at"],
        "progress": row["progress"], "progress_conf": row["progress_conf"],
        "muted_until": row["muted_until"], "meta": json.loads(row["meta"] or "{}"),
    }
    return goal


class GoalStore:
    def __init__(self, db_path):
        self.db_path = db_path
        with _connect(db_path) as conn:
            conn.executescript(SCHEMA)

    # ---------- 写 ----------

    def add(self, title, owner="user", source="user_explicit", success=None,
            deadline=None, cadence_days=7, importance=3, origin_ids=None, created_at=None):
        title = " ".join((title or "").split())[:120]
        if not title:
            return None
        if owner not in OWNERS:
            owner = "user"
        if source not in SOURCES:
            source = "user_explicit"
        if owner == "self" and len(self.active(owner="self")) >= MAX_SELF_GOALS:
            return None
        stamp = created_at or _now()
        goal = {
            "id": f"goal_{uuid.uuid4().hex[:10]}", "title": title, "owner": owner,
            "source": source, "origin_ids": origin_ids or [],
            "success": success or {"kind": "checkin"},
            "deadline": deadline, "cadence_days": max(1, int(cadence_days)),
            "importance": max(1, min(5, int(importance))), "status": "active",
            "created_at": stamp, "last_progress_at": stamp,
            "progress": 0.0, "progress_conf": 0.2, "muted_until": None, "meta": {},
        }
        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO goals(id,title,owner,source,origin_ids,success,deadline,cadence_days,"
                "importance,status,created_at,last_progress_at,progress,progress_conf,muted_until,meta) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (goal["id"], goal["title"], goal["owner"], goal["source"],
                 json.dumps(goal["origin_ids"], ensure_ascii=False),
                 json.dumps(goal["success"], ensure_ascii=False),
                 goal["deadline"], goal["cadence_days"], goal["importance"], goal["status"],
                 goal["created_at"], goal["last_progress_at"], goal["progress"],
                 goal["progress_conf"], None, "{}"))
        return self.find(goal["id"])

    def set_progress(self, goal_id, progress, note="", trusted=True):
        """进度只会往上走（除非显式重置）。手动更新是最强信号。"""
        value = max(0.0, min(1.0, float(progress)))
        stamp = _now()
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                return None
            meta = json.loads(row["meta"] or "{}")
            if note:
                history = meta.get("history") or []
                history.append({"ts": stamp, "progress": value, "note": note[:120]})
                meta["history"] = history[-20:]
            conn.execute(
                "UPDATE goals SET progress=?, progress_conf=?, last_progress_at=?, meta=? WHERE id=?",
                (value, 0.9 if trusted else 0.5, stamp,
                 json.dumps(meta, ensure_ascii=False), goal_id))
        return self.find(goal_id)

    def set_status(self, goal_id, status, extra=None):
        if status not in STATUSES:
            raise ValueError(f"不认识的 status：{status}")
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT meta FROM goals WHERE id=?", (goal_id,)).fetchone()
            if row is None:
                return None
            meta = json.loads(row["meta"] or "{}")
            if extra:
                meta.update(extra)
            conn.execute("UPDATE goals SET status=?, meta=? WHERE id=?",
                         (status, json.dumps(meta, ensure_ascii=False), goal_id))
        return self.find(goal_id)

    def mute(self, goal_id, days=14):
        """用户说「先放着」——这是最强的一条负信号，必须真的冷却。"""
        until = (datetime.now(LOCAL_TZ) + timedelta(days=int(days))).isoformat(timespec="seconds")
        with _connect(self.db_path) as conn:
            conn.execute("UPDATE goals SET muted_until=? WHERE id=?", (until, goal_id))
        return self.find(goal_id)

    # ---------- 读 ----------

    def find(self, goal_id):
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM goals WHERE id=?", (goal_id,)).fetchone()
        return _row_to_goal(row) if row else None

    def all(self, status=None):
        sql = "SELECT * FROM goals"
        params = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        with _connect(self.db_path) as conn:
            rows = conn.execute(sql + " ORDER BY created_at", params).fetchall()
        return [_row_to_goal(row) for row in rows]

    def active(self, owner=None):
        goals = [goal for goal in self.all("active")]
        if owner:
            goals = [goal for goal in goals if goal["owner"] == owner]
        return goals

    def is_muted(self, goal, now=None):
        until = memrank._parse(goal.get("muted_until"))
        if until is None:
            return False
        return (now or datetime.now(LOCAL_TZ)) < until


# ---------- 判据 ----------

def evaluate(goal, memory_texts=None, now=None):
    """到这个目标该到哪儿了：返回 (observed, evidence)。

    observed 为 True/False 表示能判；None 表示"这个判据我判不了"
    （checkin 或者写错了）。判不了不等于没进展——这个区分很重要，
    否则所有模糊目标都会被当成停滞然后反复追问。
    """
    spec = goal.get("success") or {}
    kind = spec.get("kind")
    if kind == "file_exists":
        path = Path(str(spec.get("path") or "")).expanduser()
        if not str(path):
            return None, "判据里没有路径"
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
        floor = int(spec.get("min_bytes") or 0)
        return (exists and size >= floor), (f"{path.name} 存在，{size} 字节" if exists else "文件还不存在")
    if kind == "file_count":
        folder = Path(str(spec.get("dir") or "")).expanduser()
        if not folder.is_dir():
            return False, "目录还不存在"
        need = int(spec.get("min") or 1)
        have = len([p for p in folder.iterdir() if p.is_file()])
        return have >= need, f"{folder.name} 里有 {have} 个文件（要求 {need}）"
    if kind == "memory_matches":
        if not memory_texts:
            return None, "没有可查的记忆"
        needle = str(spec.get("text") or "")
        query = memrank.grams(needle)
        best = 0.0
        for text in memory_texts:
            best = max(best, memrank.overlap(query, memrank.grams(text)))
        floor = float(spec.get("min_overlap") or 0.5)
        return best >= floor, f"记忆里最像的一条重合 {best:.0%}"
    return None, "这个判据只能问本人（checkin）"


def reminder_to_goal(text, when=None, reminder_id=None):
    """把一条提醒升级成目标。

    提醒只记时间点，所以判据默认给 checkin——但调用方可以换成
    file_exists，那样它就真的能被自动核对。
    """
    return {
        "title": text,
        "success": {"kind": "checkin"},
        "deadline": when,
        "origin_ids": [reminder_id] if reminder_id else [],
        "source": "user_explicit",
        "owner": "user",
    }
