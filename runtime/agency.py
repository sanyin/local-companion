"""动作阶梯：能动性落在"动到什么程度"上，不是"动不动"。

一、为什么要有阶梯
    纯正确性最优策略是"不确定就问、没把握就等"，那恰好杀死能动性。
    解法不是二选一，是分级：可逆的那几级放开，不可逆的永远留着人。
    所以真正的开关不是"要不要主动"，是"这一轮允许动到第几级"。

二、默认 A0
    动作只能逐级上升，每一级都要写清楚理由。反过来——默认动、被拦下来——
    那个系统迟早会做出你没授权的事。

三、A5 不可逆，永不自动
    发消息、删东西、花钱、提交代码。这道门没有"可配置的例外"。
    能自动化的边界必须在代码里，不在提示词里。

四、失败降级，不升级
    A4 失败就回 A0 记录，绝不重试、不换更激进的手段。
"""

import json
import sqlite3
from contextlib import contextmanager
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TZ = timezone(timedelta(hours=8))

A0, A1, A2, A3, A4, A5 = "A0", "A1", "A2", "A3", "A4", "A5"
LEVELS = {
    A0: "观察，只写日志",
    A1: "后台准备（草稿、清单、索引）",
    A2: "等用户在场时汇报",
    A3: "提方案 + 理由，等一句「好」",
    A4: "执行可逆动作（白名单内）",
    A5: "不可逆动作（永远人工）",
}
MODES = ("off", "observe", "propose", "act")
MAX_LEVEL_BY_MODE = {"off": A0, "observe": A1, "propose": A3, "act": A4}

MIN_SEVERITY = 0.3

SCHEMA = """
CREATE TABLE IF NOT EXISTS agency_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agency_log (
  id      INTEGER PRIMARY KEY,
  ts      TEXT NOT NULL,
  level   TEXT NOT NULL,
  goal_id TEXT,
  reason  TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_agency_log_ts ON agency_log(ts);
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


def week_key(moment=None):
    moment = moment or _now()
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def day_key(moment=None):
    return (moment or _now()).date().isoformat()


class Agency:
    def __init__(self, db_path, mode="observe", speak_per_week=3,
                 acts_per_day=5, whitelist=None):
        self.db_path = db_path
        self.mode = mode if mode in MODES else "observe"
        self.speak_per_week = max(0, int(speak_per_week))
        self.acts_per_day = max(0, int(acts_per_day))
        self.whitelist = [str(p) for p in (whitelist or [])]
        self._lock = threading.RLock()
        with _connect(db_path) as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO agency_state(key,value) VALUES('mode',?)", (self.mode,))

    # ---------- 状态 ----------

    def _get(self, key, default=None):
        with _connect(self.db_path) as conn:
            row = conn.execute("SELECT value FROM agency_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set(self, key, value):
        with _connect(self.db_path) as conn:
            conn.execute("INSERT OR REPLACE INTO agency_state(key,value) VALUES(?,?)", (key, str(value)))

    def set_mode(self, mode):
        if mode not in MODES:
            raise ValueError(f"不认识的 mode：{mode}")
        self.mode = mode
        self._set("mode", mode)
        return mode

    def spoken_this_week(self, now=None):
        key = f"spoke:{week_key(now)}"
        return int(self._get(key, 0) or 0)

    def acts_today(self, now=None):
        key = f"act:{day_key(now)}"
        return int(self._get(key, 0) or 0)

    def can_speak(self, now=None):
        return self.spoken_this_week(now) < self.speak_per_week

    def can_act(self, now=None):
        return self.acts_today(now) < self.acts_per_day

    def spend_speak(self, now=None):
        key = f"spoke:{week_key(now)}"
        with self._lock:
            used = int(self._get(key, 0) or 0) + 1
            self._set(key, used)
        return used

    def spend_act(self, now=None):
        key = f"act:{day_key(now)}"
        with self._lock:
            used = int(self._get(key, 0) or 0) + 1
            self._set(key, used)
        return used

    # ---------- 选级 ----------

    def choose(self, gap, ctx=None):
        """返回 {"level": "A2", "reason": "..."}。

        ctx 里放调用方知道的事实：用户在不在、安不安静、有没有方案、
        这个动作可不可逆、路径在不在白名单。不在 ctx 里瞎猜。
        """
        ctx = ctx or {}
        severity = float((gap or {}).get("severity") or 0.0)
        ceiling = MAX_LEVEL_BY_MODE.get(self.mode, A0)

        if self.mode == "off":
            return _pick(A0, "手刹：agency.mode=off")
        if severity < MIN_SEVERITY:
            return _pick(A0, f"差值只有 {severity:.2f}，不值得打扰")
        if ctx.get("quiet") or ctx.get("user_just_spoke"):
            return _pick(self._cap(A1, ceiling), "不打扰的时刻，先备着")
        if ctx.get("user_present") and self.can_speak():
            return _pick(self._cap(A2, ceiling),
                         f"用户在场，本周还可开口 {self.speak_per_week - self.spoken_this_week()} 次")
        # A4 必须在 A3 前面判断：mode=act 时"能做就做"优先于"先提出来问"。
        # 顺序错了，A4 永远不可达，act 模式就名存实亡。
        if (self.mode == "act" and ctx.get("has_plan") and ctx.get("reversible")
                and ctx.get("whitelist_ok") and self.can_act()
                and self._cap(A4, ceiling) == A4):
            return _pick(A4, "可逆、在白名单内、今天动作额度还没用完")
        if ctx.get("has_plan") and self._cap(A3, ceiling) == A3:
            return _pick(A3, "有具体方案，可以提出来等他点头")
        if self.can_act() and self._cap(A1, ceiling) == A1:
            return _pick(A1, "先做点可逆的准备，不打扰")
        return _pick(A0, "没有可走的层级，默认观察")

    @staticmethod
    def _cap(level, ceiling):
        order = [A0, A1, A2, A3, A4, A5]
        return level if order.index(level) <= order.index(ceiling) else ceiling

    def in_whitelist(self, path):
        """A4 只能碰白名单里的路径。空白名单 = 什么都不许动。"""
        if not self.whitelist:
            return False
        try:
            target = Path(path).resolve()
        except OSError:
            return False
        for prefix in self.whitelist:
            try:
                root = Path(prefix).resolve()
            except OSError:
                continue
            if target == root or root in target.parents:
                return True
        return False

    # ---------- 审计 ----------

    def log(self, level, goal_id=None, reason="", payload=None):
        with _connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO agency_log(ts,level,goal_id,reason,payload) VALUES (?,?,?,?,?)",
                (_now().isoformat(timespec="seconds"), level, goal_id, reason or "",
                 json.dumps(payload or {}, ensure_ascii=False)))
        return True

    def recent_log(self, limit=20):
        with _connect(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM agency_log ORDER BY id DESC LIMIT ?",
                                (int(limit),)).fetchall()
        return [{"ts": row["ts"], "level": row["level"], "goal_id": row["goal_id"],
                 "reason": row["reason"], "payload": json.loads(row["payload"] or "{}")}
                for row in rows]

    def status(self, now=None):
        now = now or _now()
        return {
            "mode": self.mode,
            "spoken_this_week": self.spoken_this_week(now),
            "speak_budget": self.speak_per_week,
            "acts_today": self.acts_today(now),
            "act_budget": self.acts_per_day,
            "whitelist": list(self.whitelist),
        }


def _pick(level, reason):
    return {"level": level, "reason": reason, "label": LEVELS.get(level, "")}
