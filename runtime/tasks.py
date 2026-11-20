"""分身：让 0 在后台干活。

为什么是"错峰"而不是"并行"：
llama.cpp 现在只开一个 slot（-np 1），模型一次只能处理一个请求。
真并行会把主人的一句话排在后台任务后面，等十几秒才回应——那是灾难。

所以策略是：
  · 主人在说话时，后台任务一律让路；
  · 主人不说话的时候，她抓紧干；
  · 每个任务都很短（有上限），保证不会长时间占着模型。

这一层不碰模型，只负责排队、调度、记录结果。跑什么由外面的 runner 决定。
"""

import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINAL = (DONE, FAILED, CANCELLED)


def _now():
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


class Task:
    def __init__(self, title, prompt, session="task"):
        self.id = f"task_{uuid.uuid4().hex[:10]}"
        self.title = (title or "").strip()[:80]
        self.prompt = prompt or ""
        self.session = session
        self.status = PENDING
        self.created_at = _now()
        self.started_at = None
        self.finished_at = None
        self.result = ""
        self.error = ""
        self.steps = []

    def as_dict(self):
        return {
            "id": self.id, "title": self.title, "status": self.status,
            "created_at": self.created_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "result": self.result,
            "error": self.error, "steps": list(self.steps),
        }


class TaskQueue:
    """一个后台工人。任务按提交顺序跑，跑不完就排队。

    runner(task, should_stop) -> str
        真正干活的地方。抛异常就算失败。
        想中途放弃就抛 TaskCancelled（或自己检查 should_stop）。
    """

    def __init__(self, runner, on_done=None, is_user_active=None,
                 keep_finished=20, idle_wait_seconds=8):
        self.runner = runner
        self.on_done = on_done
        self.is_user_active = is_user_active or (lambda: False)
        self.keep_finished = max(1, int(keep_finished))
        self.idle_wait_seconds = idle_wait_seconds

        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._tasks = {}          # id -> Task
        self._order = []          # 提交顺序
        self._stop = threading.Event()
        self._thread = None

    # ---------- 提交 ----------

    def submit(self, title, prompt, session="task"):
        with self._lock:
            task = Task(title, prompt, session=session)
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._trim()
            self._wake.notify_all()
            return task.as_dict()

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.status in TERMINAL:
                return False
            task.status = CANCELLED
            task.finished_at = _now()
            task.error = "主人叫停了"
            self._wake.notify_all()
        if self.on_done:
            self.on_done(self._tasks[task_id].as_dict())
        return True

    def cancel_all(self):
        with self._lock:
            ids = [t.id for t in self._tasks.values() if t.status in (PENDING, RUNNING)]
        return sum(1 for i in ids if self.cancel(i))

    # ---------- 查询 ----------

    def list(self):
        with self._lock:
            return [self._tasks[i].as_dict() for i in self._order if i in self._tasks]

    def get(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            return task.as_dict() if task else None

    def stats(self):
        with self._lock:
            counts = {}
            for task in self._tasks.values():
                counts[task.status] = counts.get(task.status, 0) + 1
            return counts

    def busy(self):
        with self._lock:
            return any(t.status == RUNNING for t in self._tasks.values())

    def clear_finished(self):
        with self._lock:
            removed = 0
            for task_id in list(self._order):
                task = self._tasks.get(task_id)
                if task and task.status in TERMINAL:
                    self._order.remove(task_id)
                    del self._tasks[task_id]
                    removed += 1
            return removed

    def _trim(self):
        finished = [i for i in self._order
                    if self._tasks.get(i) and self._tasks[i].status in TERMINAL]
        while len(finished) > self.keep_finished:
            victim = finished.pop(0)
            self._order.remove(victim)
            self._tasks.pop(victim, None)

    # ---------- 干活 ----------

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        with self._lock:
            self._wake.notify_all()

    def _next_pending(self):
        with self._lock:
            for task_id in self._order:
                task = self._tasks.get(task_id)
                if task and task.status == PENDING:
                    return task
        return None

    def _loop(self):
        while not self._stop.is_set():
            task = self._next_pending()
            if task is None:
                with self._lock:
                    self._wake.wait(timeout=1.0)
                continue

            # 主人在说话就让路。等一小会儿再看，别一上来就抢模型。
            while not self._stop.is_set() and self.is_user_active():
                time.sleep(self.idle_wait_seconds)

            with self._lock:
                if task.status != PENDING:
                    continue          # 排队期间被叫停了
                task.status = RUNNING
                task.started_at = _now()

            try:
                result = self.runner(task, self._stop.is_set)
                with self._lock:
                    if task.status == CANCELLED:
                        continue
                    task.status = DONE
                    task.result = (result or "").strip()
                    task.finished_at = _now()
            except Exception as exc:
                with self._lock:
                    if task.status == CANCELLED:
                        continue
                    task.status = FAILED
                    task.error = str(exc)[:300]
                    task.finished_at = _now()

            snapshot = task.as_dict()
            with self._lock:
                # 完成之后也顺手修剪，否则要等下一次提交才清
                self._trim()
            if self.on_done:
                try:
                    self.on_done(snapshot)
                except Exception:
                    pass


class TaskCancelled(Exception):
    """runner 里主动放弃时抛这个。"""
