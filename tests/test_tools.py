import json
import sys
import os
import time
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from reminder import ReminderStore
from tools import ToolBox, ToolError


class ToolBoxTests(unittest.TestCase):
    def setUp(self):
        import tools as tools_module

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "notes.txt").write_text("会议记录：周五交报告\n别忘了改预算", encoding="utf-8")
        (self.root / "draft.txt").write_text("草稿", encoding="utf-8")
        (self.root / "sub").mkdir()
        self.box = ToolBox([self.root], ReminderStore(self.root / "reminders.json"))
        # 搜索审计日志也别写进真 logs/
        self._search_log = tools_module.SEARCH_LOG
        tools_module.SEARCH_LOG = self.root / "searches.jsonl"

    def tearDown(self):
        import tools as tools_module

        tools_module.SEARCH_LOG = self._search_log
        self.tmp.cleanup()

    # ---------- 授权边界 ----------

    def test_outside_root_is_refused(self):
        with self.assertRaises(ToolError):
            self.box.resolve("C:\\Windows\\System32\\drivers\\etc\\hosts")

    def test_parent_escape_is_refused(self):
        with self.assertRaises(ToolError):
            self.box.resolve(str(self.root / ".." / ".." / "whatever.txt"))

    def test_inside_root_is_allowed(self):
        self.assertEqual(self.box.resolve("notes.txt"), (self.root / "notes.txt").resolve())

    def test_no_roots_means_no_files(self):
        box = ToolBox([], ReminderStore(self.root / "r.json"))
        with self.assertRaises(ToolError):
            box.resolve("notes.txt")

    # ---------- 安全工具 ----------

    def test_get_time_carries_a_citation(self):
        result = self.box.invoke("get_time", {})
        self.assertTrue(result.citations)
        self.assertIn("系统时间", result.citations[0])

    def test_read_file(self):
        result = self.box.invoke("read_file", {"path": "notes.txt"})
        self.assertIn("周五交报告", result.content)
        self.assertIn("文件", result.citations[0])

    def test_list_dir(self):
        result = self.box.invoke("list_dir", {"path": str(self.root)})
        self.assertIn("notes.txt", result.content)

    def test_search_files_by_content(self):
        result = self.box.invoke("search_files", {"query": "预算"})
        self.assertIn("notes.txt", result.content)

    def test_search_files_by_name(self):
        result = self.box.invoke("search_files", {"query": "draft"})
        self.assertIn("draft.txt", result.content)

    def test_search_with_no_hits_is_honest(self):
        result = self.box.invoke("search_files", {"query": "绝对不存在的词"})
        self.assertIn("没有找到", result.content)

    def test_add_reminder(self):
        result = self.box.invoke("add_reminder", {"text": "交报告", "when": "+30m"})
        self.assertIn("交报告", result.content)
        self.assertEqual(len(self.box.reminders.all()), 1)

    # ---------- 危险工具：确认门 ----------

    def test_delete_cannot_run_directly(self):
        with self.assertRaises(ToolError):
            self.box.invoke("delete_file", {"path": "draft.txt"})
        self.assertTrue((self.root / "draft.txt").exists())

    def test_write_cannot_run_directly(self):
        with self.assertRaises(ToolError):
            self.box.invoke("write_file", {"path": "new.txt", "content": "x"})
        self.assertFalse((self.root / "new.txt").exists())

    def test_stage_then_execute_deletes(self):
        action = self.box.stage("delete_file", {"path": "draft.txt"})
        self.assertIn("draft.txt", action["summary"])
        self.assertIn("不可恢复", action["summary"])
        result = self.box.execute(action["id"])
        self.assertTrue(result.ok)
        self.assertFalse((self.root / "draft.txt").exists())

    def test_action_is_single_use(self):
        action = self.box.stage("delete_file", {"path": "draft.txt"})
        self.box.execute(action["id"])
        with self.assertRaises(ToolError):
            self.box.execute(action["id"])

    def test_cancel_prevents_execution(self):
        action = self.box.stage("delete_file", {"path": "draft.txt"})
        self.assertTrue(self.box.cancel(action["id"]))
        with self.assertRaises(ToolError):
            self.box.execute(action["id"])
        self.assertTrue((self.root / "draft.txt").exists())

    def test_staged_tool_cannot_escape_roots(self):
        with self.assertRaises(ToolError):
            self.box.stage("delete_file", {"path": "C:\\Windows\\win.ini"})

    def test_write_after_confirmation(self):
        action = self.box.stage("write_file", {"path": "out.txt", "content": "写进去"})
        self.box.execute(action["id"])
        self.assertEqual((self.root / "out.txt").read_text(encoding="utf-8"), "写进去")

    def test_pending_is_visible_before_execution(self):
        action = self.box.stage("delete_file", {"path": "draft.txt"})
        self.assertEqual([a["id"] for a in self.box.pending()], [action["id"]])

    # ---------- 联网工具 ----------

    def test_weather_tool_uses_configured_city(self):
        import web
        original = web.weather_text
        web.weather_text = lambda city, days=1: f"{city} 现在晴，20°C"
        try:
            result = self.box.invoke("get_weather", {})
        finally:
            web.weather_text = original
        self.assertIn("晴", result.content)
        self.assertIn("天气", result.citations[0])

    def test_weather_tool_reports_failure_honestly(self):
        import web
        original = web.weather_text

        def boom(city, days=1):
            raise web.WebError("对方返回 502")

        web.weather_text = boom
        try:
            with self.assertRaises(ToolError) as ctx:
                self.box.invoke("get_weather", {})
        finally:
            web.weather_text = original
        self.assertIn("天气没查到", str(ctx.exception))
        self.assertIn("502", str(ctx.exception))

    def test_headlines_tool(self):
        import web
        original = web.headlines
        web.headlines = lambda limit=6: ["第一条", "第二条"]
        try:
            result = self.box.invoke("get_headlines", {})
        finally:
            web.headlines = original
        self.assertIn("第一条", result.content)

    def test_search_tool(self):
        import web
        original = web.search
        web.search = lambda query, limit=5: [
            {"title": "结果标题", "snippet": "结果摘要", "url": "https://example.com"}]
        try:
            result = self.box.invoke("search_web", {"query": "随便"})
        finally:
            web.search = original
        self.assertIn("结果标题", result.content)
        self.assertIn("搜索", result.citations[0])

    def test_network_tools_do_not_need_file_permission(self):
        # 没授权任何目录时，联网和提醒仍然可用——它们不碰本地文件
        box = ToolBox([], ReminderStore(self.root / "r2.json"))
        self.assertNotIn("get_weather", [n for n in ("delete_file",)])
        self.assertTrue(box.has_roots() is False)
        self.assertEqual(box.permission("get_weather"), "safe")
        self.assertEqual(box.permission("search_web"), "safe")

    # ---------- 规格 ----------

    def test_specs_cover_every_permission_entry(self):
        names = {spec["function"]["name"] for spec in self.box.specs()}
        self.assertEqual(names, set(self.box.PERMISSION))

    def test_permissions_are_declared_for_every_tool(self):
        for spec in self.box.specs():
            self.assertIn(spec["function"]["name"], self.box.PERMISSION)

    def test_network_tools_are_never_confirm_gated(self):
        # 查天气/查新闻是只读的，不该让主人点确认
        for name in ("get_weather", "get_headlines", "search_web"):
            self.assertEqual(self.box.permission(name), "safe", name)

    # ---------- 搜索：唯一一条能把数据送出机器的通道 ----------

    def test_search_query_too_long_is_refused(self):
        with self.assertRaises(ToolError) as ctx:
            self.box.invoke("search_web", {"query": "长" * 201})
        self.assertIn("太长", str(ctx.exception))

    def test_search_can_be_staged_for_confirmation(self):
        import tools as tools_module
        import web

        original_search, original_log = web.search, tools_module.SEARCH_LOG
        web.search = lambda query, limit=5: [
            {"title": "结果", "snippet": "摘要", "url": "https://example.com"}]
        tools_module.SEARCH_LOG = Path(self.tmp.name) / "searches.jsonl"
        try:
            # 安全工具默认不许挂起
            with self.assertRaises(ToolError):
                self.box.stage("search_web", {"query": "普通搜索"})

            action = self.box.stage("search_web",
                                    {"query": "帮我搜 310101199001011234"},
                                    force=True)
            self.assertIn("310101199001011234", action["summary"])
            result = self.box.execute(action["id"])
            self.assertIn("结果", result.content)

            logged = json.loads(tools_module.SEARCH_LOG.read_text(encoding="utf-8").strip())
            self.assertIn("310101199001011234", logged["query"])
        finally:
            web.search, tools_module.SEARCH_LOG = original_search, original_log


    # ---------- 回归：待确认队列的两处旧 bug ----------

    def test_staged_ids_do_not_collide_after_cancel(self):
        # 旧实现用 len(pending)+1 拼 id：cancel 掉一条之后再挂一条，
        # 新 id 会和还排在队里的那条撞上，前面那条被静默覆盖。
        first = self.box.stage("delete_file", {"path": "draft.txt"})
        second = self.box.stage("delete_file", {"path": "draft.txt"})
        self.box.cancel(first["id"])
        third = self.box.stage("delete_file", {"path": "draft.txt"})
        self.assertNotEqual(second["id"], third["id"])
        self.assertIn(second["id"], [action["id"] for action in self.box.pending()])

    def test_expired_action_stays_cancellable(self):
        import tools as tools_module

        original = tools_module.PENDING_TTL_SECONDS
        tools_module.PENDING_TTL_SECONDS = -1
        try:
            action = self.box.stage("delete_file", {"path": "draft.txt"})
            with self.assertRaises(ToolError) as ctx:
                self.box.execute(action["id"])
            self.assertIn("等太久", str(ctx.exception))
            # 过期的条目还在队列里，能取消；不是"已经执行过"那种一次性消失。
            self.assertTrue(self.box.cancel(action["id"]))
        finally:
            tools_module.PENDING_TTL_SECONDS = original

    def test_private_project_files_are_refused(self):
        # 把 . 整个授权出去，也不该等于把令牌/配置/记忆交出去。
        from tools import PROJECT_ROOT

        box = ToolBox([PROJECT_ROOT])
        for relative in ("runtime/config.json", "memory/memories.json", "runtime/.zero_token"):
            with self.assertRaises(ToolError):
                box.resolve(PROJECT_ROOT / relative)

    def test_search_does_not_read_private_dirs(self):
        import tools as tools_module

        original = tools_module.PROJECT_ROOT
        fake_root = Path(self.tmp.name) / "proj"
        (fake_root / "memory").mkdir(parents=True)
        (fake_root / "memory" / "memories.json").write_text("秘密内容", encoding="utf-8")
        (fake_root / "notes.txt").write_text("公开内容", encoding="utf-8")
        tools_module.PROJECT_ROOT = fake_root
        try:
            box = ToolBox([fake_root])
            self.assertNotIn("memories.json",
                             box.invoke("search_files", {"query": "秘密"}).content)
            self.assertIn("notes.txt",
                          box.invoke("search_files", {"query": "公开"}).content)
        finally:
            tools_module.PROJECT_ROOT = original




class FindThingsTests(unittest.TestCase):
    """找东西：私人助理最日常的活。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "docs"
        self.root.mkdir()
        self.box = ToolBox([str(self.root)], city="上海")
        # 老文件：内容命中
        old = self.root / "random.txt"
        old.write_text("这里提到了一次预算的事情", encoding="utf-8")
        eight_days = time.time() - 8 * 86400
        os.utime(old, (eight_days, eight_days))
        # 新文件：文件名命中
        fresh = self.root / "预算方案.md"
        fresh.write_text("明年预算的草案", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_filename_hit_ranks_above_content_hit(self):
        result = self.box.invoke("search_files", {"query": "预算"})
        first = result.content.splitlines()[1]
        self.assertIn("预算方案.md", first)

    def test_reports_how_recent(self):
        result = self.box.invoke("search_files", {"query": "预算"})
        self.assertIn("改过", result.content)

    def test_multi_word_query_requires_both(self):
        (self.root / "两者都有.md").write_text("预算 和 方案 都在这里", encoding="utf-8")
        result = self.box.invoke("search_files", {"query": "预算 方案"})
        self.assertIn("两者都有.md", result.content)

    def test_suggests_when_nothing_found(self):
        result = self.box.invoke("search_files", {"query": "预算方案"})
        self.assertIn("预算方案.md", result.content)

    def test_no_match_says_so_without_guessing(self):
        result = self.box.invoke("search_files", {"query": "完全不存在的词zzz"})
        self.assertIn("没有找到", result.content)

    def test_limit_is_respected(self):
        for i in range(10):
            (self.root / f"预算{i}.md").write_text("预算", encoding="utf-8")
        result = self.box.invoke("search_files", {"query": "预算", "limit": 3})
        listed = [line for line in result.content.splitlines() if line.startswith(str(self.root))]
        self.assertLessEqual(len(listed), 3)


if __name__ == "__main__":
    unittest.main()
