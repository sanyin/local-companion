import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import desk


class TitleTests(unittest.TestCase):
    def test_short_title_kept(self):
        self.assertEqual(desk.shorten_title("记事本"), "记事本")

    def test_long_title_trimmed(self):
        got = desk.shorten_title("x" * 100, limit=10)
        self.assertEqual(len(got), 10)
        self.assertTrue(got.endswith("…"))

    def test_whitespace_collapsed(self):
        self.assertEqual(desk.shorten_title("a   b\n c"), "a b c")

    def test_friendly_app_names(self):
        self.assertEqual(desk.pretty_app("msedge.exe"), "Edge")
        self.assertEqual(desk.pretty_app("CODE.EXE"), "VS Code")

    def test_unknown_app_falls_back(self):
        self.assertIn("Weird", desk.pretty_app("weird.exe"))


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "usage.json"
        self.focus = {"process": "code.exe", "title": "main.py - 0"}
        self.idle_seconds = 0.0
        self.now = 1_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **kw):
        return desk.DeskWatcher(
            self.path,
            sample_seconds=20,
            probe=lambda: dict(self.focus),
            idle=lambda: self.idle_seconds,
            clock=lambda: self.now,
            **kw,
        )

    def test_reports_current_window(self):
        w = self.make()
        w.sample()
        state = w.current()
        self.assertEqual(state["process"], "code.exe")
        self.assertEqual(state["title"], "main.py - 0")

    def test_accumulates_time_per_app(self):
        w = self.make()
        w.sample()
        self.now += 20
        w.sample()
        usage = dict(w.today())
        self.assertIn("code.exe", usage)
        self.assertGreater(usage["code.exe"], 0)

    def test_switching_apps_splits_time(self):
        w = self.make()
        w.sample()
        self.now += 20
        w.sample()
        self.focus = {"process": "msedge.exe", "title": "网页"}
        self.now += 20
        w.sample()
        self.now += 20
        w.sample()
        usage = dict(w.today())
        self.assertIn("code.exe", usage)
        self.assertIn("msedge.exe", usage)

    def test_idle_time_does_not_count_as_usage(self):
        w = self.make()
        w.sample()
        self.idle_seconds = 3600          # 人不在
        self.now += 20
        w.sample()
        self.assertEqual(dict(w.today()), {})

    def test_focus_minutes_resets_on_switch(self):
        w = self.make()
        w.sample()
        self.now += 600
        w.sample()
        self.assertGreaterEqual(w.current()["focus_minutes"], 10)
        self.focus = {"process": "chrome.exe", "title": "别的"}
        w.sample()
        self.assertLess(w.current()["focus_minutes"], 1)

    def test_describe_says_where_he_is(self):
        w = self.make()
        w.sample()
        text = w.describe()
        self.assertIn("main.py", text)

    def test_describe_mentions_absence(self):
        w = self.make()
        w.sample()
        self.idle_seconds = 900
        w.sample()
        self.assertIn("离开", w.describe())

    def test_persists_process_names_not_titles(self):
        w = self.make()
        w.sample()
        self.now += 20
        w.sample()
        w.save(force=True)
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("code.exe", raw)
        self.assertNotIn("main.py", raw, "窗口标题不该落盘")

    def test_reload_keeps_today_totals(self):
        w = self.make()
        w.sample()
        self.now += 20
        w.sample()
        w.save(force=True)
        again = self.make()
        again.load()
        self.assertIn("code.exe", dict(again.today()))

    def test_day_rollover_drops_previous_totals(self):
        w = self.make()
        w.sample()
        self.now += 20
        w.sample()
        self.now += 20
        w.sample()
        before = dict(w.today()).get("code.exe", 0)
        self.assertGreater(before, 0)
        original = desk._today
        try:
            desk._today = lambda: "2099-01-01"
            self.now += 20
            w.sample()
        finally:
            desk._today = original
        # 跨天之后只剩这一笔，昨天的累计不该带过来
        after = dict(w.today()).get("code.exe", 0)
        self.assertLess(after, before)

    def test_corrupt_usage_file_is_ignored(self):
        self.path.write_text("{not json", encoding="utf-8")
        w = self.make()
        w.load()
        self.assertEqual(dict(w.today()), {})

    def test_probe_failure_does_not_crash(self):
        w = desk.DeskWatcher(self.path, probe=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                             idle=lambda: 0.0, clock=lambda: 1.0)
        with self.assertRaises(RuntimeError):
            w.sample()

    def test_blind_sample_keeps_last_known_window(self):
        # 前台偶尔拿不到（隐藏控制台、受保护进程）。不能因此把显示变成 unknown，
        # 否则她会对主人说"你在 unknown.exe"。
        w = self.make()
        w.sample()
        self.focus = {"process": "unknown.exe", "title": ""}
        self.now += 20
        w.sample()
        self.assertEqual(w.current()["process"], "code.exe")

    def test_blind_sample_still_attributes_to_last_known_app(self):
        w = self.make()
        w.sample()
        self.now += 20
        w.sample()
        before = dict(w.today()).get("code.exe", 0)
        self.focus = {"process": "unknown.exe", "title": ""}
        self.now += 20
        w.sample()
        usage = dict(w.today())
        self.assertNotIn("unknown.exe", usage, "看不见的时候不该给 unknown 记时长")
        # 人还在电脑前，这 20 秒就记在上一个认识的程序上，这是最合理的估计
        self.assertGreater(usage.get("code.exe", 0), before)

    def test_unknown_when_nothing_known_yet(self):
        w = desk.DeskWatcher(self.path, probe=lambda: {"process": "", "title": ""},
                             idle=lambda: 0.0, clock=lambda: 1.0)
        w.sample()
        self.assertEqual(w.current()["process"], "unknown.exe")
        self.assertEqual(dict(w.today()), {})


class ToolIntegrationTests(unittest.TestCase):
    def test_tool_reports_when_watch_is_off(self):
        from tools import ToolBox, ToolError
        box = ToolBox([], city="上海")
        with self.assertRaises(ToolError) as ctx:
            box.invoke("what_am_i_doing", {})
        self.assertIn("在看着", str(ctx.exception))

    def test_tool_works_with_watcher(self):
        from tools import ToolBox
        class Fake:
            def describe(self):
                return "你正在 VS Code。"
        box = ToolBox([], city="上海", watcher=Fake())
        result = box.invoke("what_am_i_doing", {})
        self.assertIn("VS Code", result.content)
        self.assertIn("桌面感知", result.citations[0])

    def test_tool_is_safe_and_registered(self):
        from tools import ToolBox
        box = ToolBox([], city="上海")
        self.assertEqual(box.permission("what_am_i_doing"), "safe")
        names = {spec["function"]["name"] for spec in box.specs()}
        self.assertIn("what_am_i_doing", names)


if __name__ == "__main__":
    unittest.main()
