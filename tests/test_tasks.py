import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import tasks
from tasks import TaskQueue


def wait_until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.done = []
        self.active_flag = False

    def make(self, runner, **kw):
        return TaskQueue(runner, on_done=self.done.append,
                         is_user_active=lambda: self.active_flag,
                         idle_wait_seconds=0.05, **kw)

    def test_runs_a_task(self):
        q = self.make(lambda task, stop: "干完了：" + task.title).start()
        try:
            submitted = q.submit("整理文件", "把桌面整理一下")
            self.assertEqual(submitted["status"], tasks.PENDING)
            self.assertTrue(wait_until(lambda: q.get(submitted["id"])["status"] == tasks.DONE))
            self.assertEqual(q.get(submitted["id"])["result"], "干完了：整理文件")
        finally:
            q.stop()

    def test_notifies_when_finished(self):
        q = self.make(lambda task, stop: "好").start()
        try:
            q.submit("甲", "x")
            self.assertTrue(wait_until(lambda: len(self.done) == 1))
            self.assertEqual(self.done[0]["status"], tasks.DONE)
        finally:
            q.stop()

    def test_failure_is_recorded_not_raised(self):
        def boom(task, stop):
            raise RuntimeError("接口挂了")
        q = self.make(boom).start()
        try:
            t = q.submit("坏任务", "x")
            self.assertTrue(wait_until(lambda: q.get(t["id"])["status"] == tasks.FAILED))
            self.assertIn("接口挂了", q.get(t["id"])["error"])
        finally:
            q.stop()

    def test_runs_in_submission_order(self):
        order = []
        def runner(task, stop):
            order.append(task.title)
            return "ok"
        q = self.make(runner).start()
        try:
            for name in ("一", "二", "三"):
                q.submit(name, "x")
            self.assertTrue(wait_until(lambda: len(order) == 3))
            self.assertEqual(order, ["一", "二", "三"])
        finally:
            q.stop()

    def test_yields_while_user_is_talking(self):
        # 主人在说话时后台任务不许抢模型
        self.active_flag = True
        started = []
        def runner(task, stop):
            started.append(time.time())
            return "ok"
        q = self.make(runner).start()
        try:
            q.submit("等人", "x")
            time.sleep(0.4)
            self.assertEqual(started, [], "主人在说话时不该开工")
            self.active_flag = False
            self.assertTrue(wait_until(lambda: len(started) == 1))
        finally:
            q.stop()

    def test_cancel_pending_task(self):
        self.active_flag = True
        ran = []
        def runner(task, stop):
            ran.append(task.title)
            return "ok"
        q = self.make(runner).start()
        try:
            t = q.submit("排队中", "x")
            self.assertTrue(q.cancel(t["id"]))
            self.active_flag = False
            time.sleep(0.3)
            self.assertEqual(ran, [], "被叫停的任务不该跑")
            self.assertEqual(q.get(t["id"])["status"], tasks.CANCELLED)
        finally:
            q.stop()

    def test_cancel_unknown_returns_false(self):
        q = self.make(lambda task, stop: "x")
        self.assertFalse(q.cancel("task_nope"))

    def test_cancel_all(self):
        self.active_flag = True
        q = self.make(lambda task, stop: "x").start()
        try:
            q.submit("甲", "x")
            q.submit("乙", "x")
            self.assertEqual(q.cancel_all(), 2)
            self.assertEqual(q.stats().get(tasks.CANCELLED), 2)
        finally:
            q.stop()

    def test_busy_reflects_running(self):
        gate = threading.Event()
        def runner(task, stop):
            gate.wait(2)
            return "ok"
        q = self.make(runner).start()
        try:
            q.submit("慢活", "x")
            self.assertTrue(wait_until(lambda: q.busy()))
            gate.set()
            self.assertTrue(wait_until(lambda: not q.busy()))
        finally:
            q.stop()

    def test_finished_tasks_are_trimmed(self):
        q = self.make(lambda task, stop: "ok", keep_finished=2).start()
        try:
            for i in range(6):
                q.submit(f"任务{i}", "x")
            self.assertTrue(wait_until(lambda: len([t for t in q.list()
                                                    if t["status"] == tasks.DONE]) == 2))
            self.assertLessEqual(len(q.list()), 4)
        finally:
            q.stop()

    def test_clear_finished(self):
        q = self.make(lambda task, stop: "ok").start()
        try:
            t = q.submit("甲", "x")
            self.assertTrue(wait_until(lambda: q.get(t["id"])["status"] == tasks.DONE))
            self.assertEqual(q.clear_finished(), 1)
            self.assertEqual(q.list(), [])
        finally:
            q.stop()

    def test_empty_queue_does_not_spin_hot(self):
        q = self.make(lambda task, stop: "ok").start()
        try:
            before = time.process_time()
            time.sleep(0.3)
            spent = time.process_time() - before
            self.assertLess(spent, 0.2, "空转时不该烧 CPU")
        finally:
            q.stop()


if __name__ == "__main__":
    unittest.main()
