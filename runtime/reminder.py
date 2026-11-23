"""提醒。

修掉上一版的两个问题：
  1. 原来用「分钟字符串相等」匹配，电脑睡过那一分钟，提醒就永久消失。
     现在用 next_fire_at 语义：到点或已经过点，都会补发，然后重新排期。
  2. 原来一次只弹一个窗、写回时可能覆盖别的进程的修改。
     现在读改写都在一把锁里做。
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TZ = timezone(timedelta(hours=8))
_LOCK = threading.RLock()


def now():
    return datetime.now(LOCAL_TZ)


def parse_when(text):
    """把「+30m」「09:00」「2026-11-16T09:00」都翻成具体时间。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("没有给时间")
    current = now()

    if text.startswith("+"):
        body = text[1:].strip().lower()
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if body and body[-1] in units:
            amount = float(body[:-1] or 0)
            return current + timedelta(seconds=amount * units[body[-1]])
        raise ValueError(f"看不懂的时间：{text}")

    if len(text) == 5 and text[2] == ":":
        hour, minute = int(text[:2]), int(text[3:])
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"看不懂的时间：{text}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed


class ReminderStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("reminders") or []
        return [item for item in raw if isinstance(item, dict) and item.get("text")]

    def _write(self, items):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, text, when, repeat="none"):
        moment = parse_when(when) if isinstance(when, str) else when
        with _LOCK:
            items = self._read()
            item = {
                "id": f"rm_{len(items) + 1}_{int(moment.timestamp())}",
                "text": " ".join((text or "").split()).strip()[:200],
                "at": moment.isoformat(timespec="minutes"),
                "next_fire_at": moment.isoformat(timespec="seconds"),
                "repeat": repeat if repeat in ("none", "daily", "weekdays") else "none",
                "enabled": True,
                "fired_count": 0,
                "last_fired_at": None,
            }
            items.append(item)
            self._write(items)
            return dict(item)

    def all(self):
        with _LOCK:
            return self._read()

    def due(self, moment=None):
        """到点的，包括**已经过点但没发过的**——电脑睡过去了也不丢。"""
        moment = moment or now()
        with _LOCK:
            items = self._read()
            return [
                dict(item) for item in items
                if item.get("enabled", True) and item.get("next_fire_at")
                and datetime.fromisoformat(item["next_fire_at"]) <= moment
            ]

    def mark_fired(self, reminder_id, moment=None):
        moment = moment or now()
        with _LOCK:
            items = self._read()
            for item in items:
                if item.get("id") != reminder_id:
                    continue
                item["last_fired_at"] = moment.isoformat(timespec="seconds")
                item["fired_count"] = int(item.get("fired_count") or 0) + 1
                repeat = item.get("repeat") or "none"
                if repeat == "none":
                    item["enabled"] = False
                    item["next_fire_at"] = None
                else:
                    base = datetime.fromisoformat(item["next_fire_at"])
                    step = timedelta(days=1)
                    following = base + step
                    while following <= moment:
                        following += step
                    if repeat == "weekdays":
                        while following.weekday() >= 5:
                            following += step
                    item["next_fire_at"] = following.isoformat(timespec="seconds")
                self._write(items)
                return dict(item)
        return None

    def remove(self, reminder_id):
        with _LOCK:
            items = self._read()
            remaining = [item for item in items if item.get("id") != reminder_id]
            if len(remaining) == len(items):
                return False
            self._write(remaining)
            return True
