"""主动开口。

分工很硬：
  · **要不要说、什么时候说** —— 这里，纯规则、可注入时钟、可测试、零延迟。
  · **说成什么语气** —— 模型，见 core.speak()。

之所以这么切：话痨比沉默更致命。一个总在说话的东西，三分钟就被关掉。
所以闸门必须在代码里，而且要有总量上限，不能交给模型自己把握。

四条规矩：
  1. 一天最多 3 次（可配）。
  2. 两次之间至少隔 90 分钟。
  3. 主人刚说过话（5 分钟内）绝不插嘴。
  4. 23:00 到 07:00 是安静时段，只有"太晚了"那一种例外。
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TZ = timezone(timedelta(hours=8))

KIND_MORNING = "morning"
KIND_RETURN = "return"
KIND_LATE_NIGHT = "late_night"
KIND_SIT = "sit"
# 下面两种是「按事」而不是「按钟点」——这是它跟闹钟的分界线
KIND_NOTICE = "notice"        # 你的事要到了
KIND_CALLBACK = "callback"    # 想起你以前说过的一件事

LABELS = {
    KIND_MORNING: "早上开口",
    KIND_RETURN: "主人回来了",
    KIND_LATE_NIGHT: "夜深了",
    KIND_SIT: "坐太久",
    KIND_NOTICE: "你的事快到了",
    KIND_CALLBACK: "想起你说过的事",
}

# 每句话背后的意思。模型只负责把意思说成她的语气。
CUES = {
    KIND_MORNING: (
        "现在是清早，主人刚打开你。**这是你在主动开口，不是在回应他。**\n"
        "把下面给你的事实，用一到两句话说给他听：先说日期，再挑最值得知道的一两件事。\n"
        "**事实只许用给你的那些，一条都不许编。**"
        "可以顺口关心一句（冷就多穿点），但别变成健康科普，也别列清单。\n"
        "不要问「有什么可以帮您」，不要超过两句。"
    ),
    KIND_RETURN: (
        "主人隔了一阵子才回来。**这是你在主动开口。**\n"
        "说一到两句，像认识很久的人那样自然地接上。"
        "可以提一句他上次在忙的事，但别连问，也别客套。\n"
        "不要问「有什么可以帮您」。"
    ),
    KIND_LATE_NIGHT: (
        "已经很晚了，主人还没睡。**这是你在主动开口。**\n"
        "说一到两句：关心，但不唠叨，不讲道理，不说教。"
        "像朋友那样提醒一句就够了，说完就停。"
    ),
    KIND_SIT: (
        "主人已经连续坐了 {sit_minutes} 分钟。**这是你在主动开口。**\n"
        "说一句话就够了：提醒他起来动一下。"
        "不要像健康科普，不要讲久坐的危害，不要说「建议您」。"
    ),
    KIND_NOTICE: (
        "主人自己设的一件事快到了。**这是你在主动开口。**\n"
        "把给你的那条事实，用一句话说给他听：**先说是哪件事，再说还有多久**。\n"
        "不要重复他的原话，不要加提醒他该怎么做的一大串建议，"
        "不要说「别忘记」——他当然知道，他只是可能正忙。"
    ),
    KIND_CALLBACK: (
        "你想起他以前跟你说过的一件事。**这是你在主动开口，不是他问的。**\n"
        "用一到两句话提一句，然后停。像真的想起来了，不是查档案。\n"
        "**不要**说「根据我的记忆」「你曾提到过」这种话——那是数据库，不是人。\n"
        "如果这件事现在提起来有点唐突，那就说得轻一点，或者只问一句进展。"
    ),
}

DEFAULT_BUDGET = 3
DEFAULT_GAP_MINUTES = 90
QUIET_START = 23      # 23 点起进入安静时段
QUIET_END = 6         # 6 点结束
MORNING_START = 6
MORNING_END = 11
SIT_THRESHOLD_MINUTES = 90
NOTICE_LEAD_MINUTES = 30      # 提前多久打招呼
RECENT_USER_GUARD_MINUTES = 5
RETURN_AFTER_HOURS = 18


def _today(now):
    return now.date().isoformat()


def _night_key(now):
    """「今晚」的主键，跨午夜要算同一个晚上。

    23:40 和次日 01:40 是同一个人熬的同一夜，不该被提醒两次。
    """
    shifted = now - timedelta(hours=2) if now.hour < 2 else now
    return shifted.date().isoformat()


def _parse(stamp):
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


class ProactivePlanner:
    """决定「这一轮要不要开口」。不联网、不碰模型，只读状态和时钟。"""

    def __init__(self, path, budget=DEFAULT_BUDGET, gap_minutes=DEFAULT_GAP_MINUTES):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.budget = max(0, int(budget))
        self.gap = timedelta(minutes=max(0, int(gap_minutes)))
        self._lock = threading.RLock()

    # ---------- 状态 ----------

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except (FileNotFoundError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        raw.setdefault("date", "")
        raw.setdefault("count", 0)
        raw.setdefault("last_at", None)
        raw.setdefault("last_open", None)
        raw.setdefault("last_night", None)
        raw.setdefault("spoken", {})
        if not isinstance(raw.get("spoken"), dict):
            raw["spoken"] = {}
        return raw

    def _save(self, state):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _roll(self, state, now):
        """跨天就把计数清零。"""
        today = _today(now)
        if state.get("date") != today:
            state["date"] = today
            state["count"] = 0
            state["spoken"] = {}
        return state

    def configure(self, budget=None, gap_minutes=None):
        """网页上改了设置，就地生效，不用重启。"""
        if budget is not None:
            self.budget = max(0, int(budget))
        if gap_minutes is not None:
            self.gap = timedelta(minutes=max(0, int(gap_minutes)))

    def snapshot(self):
        with self._lock:
            return self._load()

    # ---------- 决策 ----------

    def decide(self, now=None, event="tick", sit_minutes=0, recent_user_minutes=999,
               notice=None, callback=None):
        """返回一个 kind（要开口）或 None（闭嘴）。

        event:   "panel_open"（主人打开了面板）或 "tick"（心跳轮询）
        notice:  {"id": 提醒 id, "minutes": 还有几分钟} —— 他的事快到了
        callback: 一段他以前说过的话 —— 想起来了就提一句
        """
        now = now or datetime.now(LOCAL_TZ)
        with self._lock:
            state = self._roll(self._load(), now)
            hour = now.hour
            today = _today(now)

            # 主人刚说过话，就别插嘴——这条排在所有规则最前面
            if recent_user_minutes < RECENT_USER_GUARD_MINUTES:
                return None
            if state["count"] >= self.budget:
                return None
            last_at = _parse(state.get("last_at"))
            if last_at and now - last_at < self.gap:
                return None

            kind = None
            # 他的事快到了，排在最前面：这不是"要不要说"，是"该说了"
            if notice and not self._said_today(state, today, f"{KIND_NOTICE}:{notice.get('id')}"):
                kind = KIND_NOTICE
            elif not self._said_today(state, today, KIND_MORNING) \
                    and MORNING_START <= hour < MORNING_END and event == "panel_open":
                kind = KIND_MORNING
            elif state.get("last_night") != _night_key(now) and (hour >= QUIET_START or hour < 2):
                kind = KIND_LATE_NIGHT
            elif event == "panel_open" and self._returning(state, now, today):
                kind = KIND_RETURN
            elif sit_minutes >= SIT_THRESHOLD_MINUTES and not self._said_today(state, today, KIND_SIT):
                kind = KIND_SIT
            elif (callback and event == "panel_open"
                  and not self._said_today(state, today, KIND_CALLBACK)):
                # 想起你以前说过的事。只在他看着的时候提，不打扰。
                kind = KIND_CALLBACK

            # 安静时段只留「太晚了」和「他自己设的事」
            if kind and kind not in (KIND_LATE_NIGHT, KIND_NOTICE) \
                    and (hour >= QUIET_START or hour < QUIET_END):
                return None
            return kind

    @staticmethod
    def _said_today(state, today, kind):
        return state["spoken"].get(kind) == today

    @staticmethod
    def _returning(state, now, today):
        last_open = _parse(state.get("last_open"))
        if not last_open:
            return False
        return (now - last_open) >= timedelta(hours=RETURN_AFTER_HOURS)

    # ---------- 记账 ----------

    def record_spoken(self, kind, now=None, key=None):
        """key 用来给「同一种由头、不同的那件事」分开记账。

        提醒 1 和提醒 2 都是 notice，但它们是两件事，要各记各的，
        否则说了一件之后另一件就被当成"今天说过了"。
        """
        now = now or datetime.now(LOCAL_TZ)
        with self._lock:
            state = self._roll(self._load(), now)
            state["count"] = int(state.get("count") or 0) + 1
            state["last_at"] = now.isoformat(timespec="seconds")
            state["spoken"][key or kind] = _today(now)
            if kind == KIND_LATE_NIGHT:
                # 单独记，因为它不能跟着跨天一起清零
                state["last_night"] = _night_key(now)
            self._save(state)

    def record_open(self, now=None):
        """记下「主人打开面板」这件事，用来判断他是不是很久没来了。"""
        now = now or datetime.now(LOCAL_TZ)
        with self._lock:
            state = self._roll(self._load(), now)
            state["last_open"] = now.isoformat(timespec="seconds")
            self._save(state)

    def remaining(self, now=None):
        now = now or datetime.now(LOCAL_TZ)
        with self._lock:
            state = self._roll(self._load(), now)
            return max(0, self.budget - int(state.get("count") or 0))
