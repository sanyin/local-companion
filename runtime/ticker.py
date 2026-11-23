"""心跳：让「现在几点」这件事不再依赖界面有没有开。

以前的心跳是借来的：桌宠每几十秒问一次 `/api/proactive/check`，core 才想起
「该不该开口」。界面一关，它就不存在了——那对"主动性"勉强够用（说了也没人听），
对"能动性"是致命的：A0/A1 本该在你不在的时候继续发生。

三条纪律：
  1. 心跳线程**不调模型**。要模型判断的丢给 TaskQueue，这里只做纯规则。
  2. 睡眠/关机之后只补一次，不补九十六次。幂等靠 last_tick。
  3. 时钟可注入。否则测试写不出来，只能靠等——等是最贵的测试。
"""

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TZ = timezone(timedelta(hours=8))

DEFAULT_INTERVAL = 300          # 5 分钟
MIN_INTERVAL = 30
CATCHUP_LIMIT_HOURS = 2.0       # 断档超过这个时长就只算"补一次"


def _now():
    return datetime.now(LOCAL_TZ)


class Ticker:
    def __init__(self, on_tick, interval=DEFAULT_INTERVAL, now_fn=None, state_path=None,
                 logger=None):
        self.on_tick = on_tick
        self.interval = max(MIN_INTERVAL, int(interval))
        self.now_fn = now_fn or _now
        self.state_path = Path(state_path) if state_path else None
        self.logger = logger or (lambda msg: None)
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.RLock()
        self.ticks = 0
        self.errors = 0

    # ---------- 状态 ----------

    def _load(self):
        if not self.state_path or not self.state_path.exists():
            return {}
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, state):
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    def last_tick(self):
        stamp = self._load().get("last_tick")
        try:
            moment = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            return None
        return moment if moment.tzinfo else moment.replace(tzinfo=LOCAL_TZ)

    # ---------- 跑 ----------

    def tick_once(self, now=None):
        """跑一次。异常一律吞掉——心跳不能因为一次失败就停。

        返回 on_tick 的结果；失败返回 {"error": "..."}。
        """
        now = now or self.now_fn()
        catch_up = False
        last = self.last_tick()
        if last is not None:
            gap_hours = (now - last).total_seconds() / 3600
            catch_up = gap_hours > CATCHUP_LIMIT_HOURS
        with self._lock:
            self.ticks += 1
            try:
                result = self.on_tick(now, catch_up=catch_up)
                if isinstance(result, dict):
                    result.setdefault("catch_up", catch_up)
            except Exception as exc:               # noqa: BLE001 —— 故意的
                self.errors += 1
                self.logger(f"tick 失败：{exc}")
                result = {"error": str(exc)[:200], "catch_up": catch_up}
            self._save({"last_tick": now.isoformat(timespec="seconds"),
                        "ticks": self.ticks, "errors": self.errors})
        return result

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="zero-ticker")
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        # 分区睡眠，否则 stop() 要等满一个 interval 才生效
        while not self._stop.is_set():
            self.tick_once()
            slept = 0.0
            while slept < self.interval and not self._stop.is_set():
                time.sleep(min(1.0, self.interval - slept))
                slept += 1.0

    def stats(self):
        return {"running": bool(self._thread and self._thread.is_alive()),
                "interval": self.interval, "ticks": self.ticks, "errors": self.errors,
                "last_tick": (self.last_tick() or None) and self.last_tick().isoformat(timespec="seconds")}


class NullTicker:
    """关掉主动行为时用它。接口一样，什么都不做——省得调用点到处判断 None。"""

    def start(self):
        return self

    def stop(self):
        pass

    def tick_once(self, now=None):
        return {"skipped": True}

    def stats(self):
        return {"running": False, "disabled": True}
