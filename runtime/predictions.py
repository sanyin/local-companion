"""预测账本：让系统记得"我上次判断错了"。

这是整条能动性链路上最缺的一环，也是记忆库真正能贡献的东西。
现在它记的全是**你**；要谈经验，它得记**自己**：

    预测：这条提醒三天内会到期        → 结果：准时
    预测：这件事他会在意              → 结果：他回了个「嗯」
    判断：这周他会推进到 40%          → 结果：还在 15%

攒够样本就能回答「我在'他会不会在意'这件事上，十次估高了七次」。
这条结论不需要改权重就能生效——它直接改阈值、改动作层级、改开口时机。

为什么不做成"学习"：
    改权重不可回滚、不可解释、出问题查不出来。校准因子是三个数的事，
    能单测、能手算、能一键置回 1.0。等真要做持续学习，这张表就是训练信号。
"""

import json
import sqlite3
from contextlib import contextmanager
import uuid
from datetime import datetime, timedelta, timezone

import memrank

LOCAL_TZ = timezone(timedelta(hours=8))

SUBJECT_KINDS = ("timing", "goal_progress", "user_interest", "outcome")
MIN_SAMPLES = 10
FACTOR_LOW, FACTOR_HIGH = 0.5, 1.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
  id           TEXT PRIMARY KEY,
  made_at      TEXT NOT NULL,
  subject_kind TEXT NOT NULL,
  subject_id   TEXT,
  statement    TEXT NOT NULL,
  p            REAL NOT NULL,
  resolve_at   TEXT NOT NULL,
  resolve_spec TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'open',
  outcome      INTEGER,
  error_kind   TEXT,
  resolved_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status, resolve_at);
"""


def _now():
    return datetime.now(LOCAL_TZ)


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


class PredictionLedger:
    def __init__(self, db_path):
        self.db_path = db_path
        with _connect(db_path) as conn:
            conn.executescript(SCHEMA)

    # ---------- 记 ----------

    def record(self, subject_kind, statement, p, resolve_at, subject_id=None, resolve_spec=None):
        if subject_kind not in SUBJECT_KINDS:
            raise ValueError(f"不认识的 subject_kind：{subject_kind}")
        statement = " ".join((statement or "").split())[:160]
        if not statement:
            return None
        moment = resolve_at if isinstance(resolve_at, str) else resolve_at.isoformat(timespec="seconds")
        pid = f"pred_{uuid.uuid4().hex[:10]}"
        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO predictions(id,made_at,subject_kind,subject_id,statement,p,"
                "resolve_at,resolve_spec,status) VALUES (?,?,?,?,?,?,?,?,'open')",
                (pid, _now().isoformat(timespec="seconds"), subject_kind, subject_id,
                 statement, max(0.01, min(0.99, float(p))), moment,
                 json.dumps(resolve_spec or {}, ensure_ascii=False)))
        return self.get(pid)

    def get(self, pid):
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone()
        return _row(row) if row else None

    def open_items(self, now=None, due_only=False):
        now = now or _now()
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM predictions WHERE status='open' ORDER BY resolve_at").fetchall()
        items = [_row(row) for row in rows]
        if due_only:
            items = [item for item in items
                     if (memrank._parse(item["resolve_at"]) or now) <= now]
        return items

    # ---------- 核 ----------

    def resolve(self, pid, outcome, error_kind=None, note=""):
        """outcome: True/False。error_kind 不给就按 p 自动判是高估还是低估。"""
        item = self.get(pid)
        if item is None:
            return None
        if error_kind is None:
            if outcome and item["p"] < 0.5:
                error_kind = "under"
            elif not outcome and item["p"] >= 0.5:
                error_kind = "over"
            else:
                error_kind = "misjudged" if not outcome else "noise"
        with _connect(self.db_path) as conn:
            conn.execute(
                "UPDATE predictions SET status='resolved', outcome=?, error_kind=?, resolved_at=? "
                "WHERE id=?",
                (1 if outcome else 0, error_kind,
                 _now().isoformat(timespec="seconds"), pid))
        resolved = self.get(pid)
        resolved["note"] = note
        return resolved

    # ---------- 校准 ----------

    def factor(self, subject_kind):
        """命中率 / 平均预测概率。估高了就小于 1，估低了就大于 1。

        样本不够时返回 1.0——宁可不动，也不要拿三条样本去改行为。
        """
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT p, outcome FROM predictions WHERE status='resolved' AND subject_kind=?",
                (subject_kind,)).fetchall()
        pairs = [(row["p"], row["outcome"]) for row in rows if row["outcome"] is not None]
        if len(pairs) < MIN_SAMPLES:
            return 1.0
        hit = sum(1 for _, outcome in pairs if outcome) / len(pairs)
        mean_p = sum(p for p, _ in pairs) / len(pairs)
        if mean_p <= 0.01:
            return 1.0
        return round(max(FACTOR_LOW, min(FACTOR_HIGH, hit / mean_p)), 3)

    def summary(self):
        with _connect(self.db_path) as conn:
            rows = conn.execute(
                "SELECT subject_kind, status, outcome, COUNT(*) AS n FROM predictions "
                "GROUP BY subject_kind, status, outcome").fetchall()
        out = {}
        for row in rows:
            bucket = out.setdefault(row["subject_kind"], {"open": 0, "hit": 0, "miss": 0})
            if row["status"] == "open":
                bucket["open"] += row["n"]
            elif row["outcome"]:
                bucket["hit"] += row["n"]
            else:
                bucket["miss"] += row["n"]
        for kind, bucket in out.items():
            bucket["factor"] = self.factor(kind)
            total = bucket["hit"] + bucket["miss"]
            bucket["hit_rate"] = round(bucket["hit"] / total, 3) if total else None
        return out


def _row(row):
    return {
        "id": row["id"], "made_at": row["made_at"], "subject_kind": row["subject_kind"],
        "subject_id": row["subject_id"], "statement": row["statement"], "p": row["p"],
        "resolve_at": row["resolve_at"], "resolve_spec": json.loads(row["resolve_spec"] or "{}"),
        "status": row["status"], "outcome": row["outcome"], "error_kind": row["error_kind"],
        "resolved_at": row["resolved_at"],
    }
