"""存储层：迁移不能丢东西，确认/替换/误删恢复这三条老规矩必须原样保住。"""

import json
import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import memdb
import memory


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.json_path = self.dir / "memories.json"
        self.db = memdb.MemDB(self.json_path)

    def tearDown(self):
        self.tmp.cleanup()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.json_path = self.dir / "memories.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_imports_old_json_and_keeps_fields(self):
        legacy = [
            {"text": "主人住在上海", "created_at": "2026-11-01T10:00:00+08:00", "pinned": True},
            {"id": "mem_abc", "text": "在准备考研", "kind": "fact", "source": "user_explicit",
             "status": "active", "importance": 4, "explicit": True,
             "created_at": "2026-11-02T10:00:00+08:00", "supersedes": []},
        ]
        self.json_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        fresh = memdb.MemDB(self.json_path)

        self.assertEqual(fresh.count(), 2)
        by_text = {item["text"]: item for item in fresh.all()}
        self.assertTrue(by_text["主人住在上海"]["pinned"])
        self.assertEqual(by_text["在准备考研"]["id"], "mem_abc")
        self.assertEqual(by_text["在准备考研"]["importance"], 4)

    def test_migration_does_not_touch_the_original_file(self):
        self.json_path.write_text(json.dumps([{"text": "主人住在上海"}], ensure_ascii=False),
                                  encoding="utf-8")
        before = self.json_path.read_text(encoding="utf-8")
        memdb.MemDB(self.json_path)
        self.assertEqual(before, self.json_path.read_text(encoding="utf-8"))

    def test_second_open_does_not_duplicate(self):
        self.json_path.write_text(json.dumps([{"text": "主人住在上海"}], ensure_ascii=False),
                                  encoding="utf-8")
        memdb.MemDB(self.json_path)
        second = memdb.MemDB(self.json_path)
        self.assertEqual(second.count(), 1)


class WriteTests(Base):
    def test_add_marks_explicit_and_counts_once(self):
        item = self.db.add("不喜欢在早上开会", kind="preference")
        self.assertEqual(item["source"], "user_explicit")
        self.db.add("不喜欢在早上开会", kind="preference")
        self.assertEqual(self.db.count(), 1)

    def test_inferred_goes_pending_then_confirm(self):
        self.db.add("可能在学日语", source="inferred", status="pending")
        self.assertEqual(len(self.db.pending()), 1)
        self.assertEqual(len(self.db.active()), 0)
        confirmed = self.db.confirm(self.db.pending()[0]["id"])
        self.assertEqual(confirmed["status"], "active")
        self.assertEqual(len(self.db.active()), 1)

    def test_confirm_retires_the_old_fact(self):
        old = self.db.add("主人住在上海")
        new = self.db.add("主人搬到北京了", source="inferred", status="pending",
                          supersedes=[old["id"]])
        self.db.confirm(new["id"])
        self.assertEqual(self.db.find(old["id"])["status"], "superseded")
        self.assertIsNotNone(self.db.find(old["id"])["valid_to"])
        self.assertEqual(self.db.find(old["id"])["superseded_by"], new["id"])

    def test_rejected_candidate_never_comes_back(self):
        self.db.add("可能在学日语", source="inferred", status="pending")
        self.db.forget(self.db.pending()[0]["id"])
        self.assertIsNone(self.db.add("可能在学日语", source="inferred", status="pending"))

    def test_forget_then_restore(self):
        item = self.db.add("主人住在上海")
        self.assertTrue(self.db.forget(item["id"]))
        self.assertEqual(self.db.count(), 0)
        self.assertEqual(len(self.db.forgotten()), 1)
        restored = self.db.restore(item["id"])
        self.assertIsNotNone(restored)
        self.assertEqual(self.db.count(), 1)


class RetrievalTests(Base):
    def test_chinese_substring_is_found(self):
        # 这是换 FTS 的理由：unicode61 会把「我住在上海」当成一个 token
        self.db.add("我住在上海")
        self.db.add("喜欢喝咖啡")
        block = self.db.prompt_block("我住哪")
        self.assertIn("上海", block.splitlines()[0])

    def test_style_is_separated_from_facts(self):
        self.db.add("别催我", kind="style")
        self.assertIn("别催我", self.db.style_block())
        self.assertNotIn("别催我", self.db.prompt_block(""))

    def test_pinned_survives_budget(self):
        for index in range(10):
            self.db.add(f"第{index}条很长的记忆" * 3)
        pinned = self.db.add("这条被置顶了", pinned=True)
        block = self.db.prompt_block("完全无关的查询", chars=200)
        self.assertIn(pinned["text"], block)

    def test_budget_by_chars(self):
        for index in range(20):
            self.db.add(f"第{index}条" + "填充" * 30)
        block = self.db.prompt_block("填充", chars=300)
        total = sum(len(line) for line in block.splitlines())
        self.assertLessEqual(total, 340)

    def test_prompt_block_is_not_empty_on_cold_start(self):
        self.assertEqual(self.db.prompt_block("随便问问"), "（还没有记下什么。）")

    def test_queries_leave_a_trace(self):
        self.db.add("主人住在上海")
        self.db.prompt_block("上海")
        with closing(sqlite3.connect(self.db.db_path)) as conn:
            rows = conn.execute("SELECT query, picked FROM retrieval_trace").fetchall()
        self.assertGreaterEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "上海")

    def test_touch_heats_and_counts(self):
        item = self.db.add("主人住在上海")
        self.db.touch_many([item["id"]])
        again = self.db.find(item["id"])
        self.assertEqual(again["use_count"], 1)
        self.assertGreater(again["heat"], 1.0)

    def test_contains_snippet_guards_web_search(self):
        self.db.add("我的身份证号是1234567890")
        self.assertIsNotNone(self.db.contains_snippet("帮我搜一下我的身份证号是1234567890"))
        self.assertIsNone(self.db.contains_snippet("帮我搜一下今天的天气"))

    def test_similar_active_finds_the_old_fact(self):
        old = self.db.add("主人住在上海")
        near = self.db.similar_active("我搬到北京了", limit=3)
        self.assertIn(old["id"], [item["id"] for item in near])


class ParityTests(Base):
    """换实现不能换行为：同样的操作序列，两个实现结果要一致。"""

    def test_same_sequence_same_shape(self):
        legacy = memory.MemoryStore(self.dir / "old.json")
        steps = [
            ("add", "主人住在上海", {}),
            ("add", "在准备考研", {"kind": "fact"}),
            ("add", "不喜欢在早上开会", {"kind": "preference"}),
        ]
        for _, text, kwargs in steps:
            legacy.add(text, **kwargs)
            self.db.add(text, **kwargs)
        self.assertEqual(sorted(item["text"] for item in legacy.active()),
                         sorted(item["text"] for item in self.db.active()))

    def test_sync_json_roundtrip(self):
        self.db.add("主人住在上海")
        target = self.dir / "out.json"
        self.db.sync_json(target)
        payload = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["text"], "主人住在上海")


if __name__ == "__main__":
    unittest.main()
