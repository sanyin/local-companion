"""差值：能动性的触发源。

闹钟由"时间到了"触发，能动性由"该到的没到"触发。这个区别就是这张表存在的理由。

    expected(t) = 按过去的推进速度，今天该到哪儿
    observed(t) = 现在实际到哪儿（目标自己的进度信念，或判据给的结果）
    slack       = expected - observed

三个防噪设计（缺一个，这东西就会变成催命鬼）：
  1. 停滞不满 7 天不产生 gap——正常的停顿不该被当停滞。
  2. 同一个目标 7 天内只产生一次——去重记在 gap_log 里，重启不丢。
  3. 用户说过「先放着」——该目标冷却 14 天，期间零 gap。

冷启动（没有任何历史速率时）只用两条能自证的规则：
  期限过了七成而进度不到三成；或者连续 14 天没有任何推进。
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memrank

LOCAL_TZ = timezone(timedelta(hours=8))

STAGNATION_DAYS = 7
HARD_STAGNATION_DAYS = 14
COLD_START_DEADLINE = 0.7
COLD_START_PROGRESS = 0.3
MIN_SLACK = 0.15
DEDUPE_DAYS = 7


def _now():
    return datetime.now(LOCAL_TZ)


def _clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


class GapEngine:
    def __init__(self, log_path=None, stagnation_days=STAGNATION_DAYS, dedupe_days=DEDUPE_DAYS):
        self.log_path = Path(log_path) if log_path else None
        self.stagnation_days = int(stagnation_days)
        self.dedupe_days = int(dedupe_days)
        self._lock = threading.RLock()
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 去重账本 ----------

    def _load(self):
        if not self.log_path or not self.log_path.exists():
            return {}
        try:
            raw = json.loads(self.log_path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, state):
        if not self.log_path:
            return
        tmp = self.log_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.log_path)

    def _recently_reported(self, goal_id, now, state):
        stamp = memrank._parse((state.get(goal_id) or {}).get("at"))
        if stamp is None:
            return False
        return (now - stamp) < timedelta(days=self.dedupe_days)

    def mark_reported(self, goal_id, now=None, severity=0.0):
        now = now or _now()
        with self._lock:
            state = self._load()
            state[goal_id] = {"at": now.isoformat(timespec="seconds"), "severity": round(severity, 3)}
            self._save(state)

    # ---------- 评估 ----------

    def evaluate(self, goals, now=None, evidence=None):
        """返回这一轮该被注意的 gap 列表。evidence: {goal_id: (observed, text)}。"""
        now = now or _now()
        evidence = evidence or {}
        gaps = []
        with self._lock:
            state = self._load()
            for goal in goals:
                if goal.get("status") != "active":
                    continue
                if _muted(goal, now):
                    continue
                gap = self._one(goal, now, evidence.get(goal["id"]), state)
                if gap:
                    gaps.append(gap)
                    state[goal["id"]] = {"at": now.isoformat(timespec="seconds"),
                                         "severity": round(gap["severity"], 3)}
            self._save(state)
        gaps.sort(key=lambda item: item["severity"], reverse=True)
        return gaps

    def _one(self, goal, now, evidence, state):
        progress = _clamp(goal.get("progress") or 0.0)
        importance = int(goal.get("importance") or 3)
        stalled_days = self._stalled_days(goal, now)

        # 判据说"已经完成"——那就不是 gap，该让上层标 done。
        if evidence and evidence[0] is True:
            return None

        expected = None
        reason = None
        deadline = memrank._parse(goal.get("deadline"))
        if deadline is not None:
            start = memrank._parse(goal.get("created_at")) or now
            span = (deadline - start).total_seconds()
            if span > 0:
                ratio = _clamp((now - start).total_seconds() / span)
                expected = ratio
                if ratio >= COLD_START_DEADLINE and progress < COLD_START_PROGRESS:
                    reason = f"期限过了 {ratio:.0%}，进度还只有 {progress:.0%}"

        if reason is None and stalled_days >= self.stagnation_days:
            # 没有期限就只有"停太久"这一条。基线取"如果按最慢的正常速度，现在该有 0.3"。
            expected = 0.3
            reason = f"已经 {stalled_days} 天没有推进了"

        if reason is None and stalled_days >= HARD_STAGNATION_DAYS:
            expected = 0.3
            reason = f"连续 {stalled_days} 天没有任何进展证据"

        if reason is None:
            return None
        if self._recently_reported(goal["id"], now, state):
            return None

        slack = max(0.0, (expected or 0.0) - progress)
        if slack < MIN_SLACK and stalled_days < self.stagnation_days:
            return None

        severity = _clamp(
            0.5 * _clamp(slack) + 0.3 * _clamp(stalled_days / float(self.stagnation_days))
            + 0.2 * (importance / 5.0))
        return {
            "goal_id": goal["id"],
            "title": goal.get("title") or "",
            "severity": round(severity, 3),
            "reason": reason,
            "expected": round(expected or 0.0, 3),
            "observed": round(progress, 3),
            "stalled_days": round(stalled_days, 1),
            "evidence": (evidence[1] if evidence and len(evidence) > 1 else ""),
        }

    @staticmethod
    def _stalled_days(goal, now):
        stamp = memrank._parse(goal.get("last_progress_at") or goal.get("created_at"))
        if stamp is None:
            return 0.0
        return max(0.0, (now - stamp).total_seconds() / 86400)


def _muted(goal, now):
    until = memrank._parse(goal.get("muted_until"))
    return until is not None and now < until
