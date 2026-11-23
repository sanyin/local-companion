import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import memory
from memory import ConversationLog, MemoryStore, extract_candidates

LOCAL_TZ = timezone(timedelta(hours=8))


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memories.json"
        self.store = MemoryStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_add_and_active(self):
        item = self.store.add("不喜欢在早上开会", kind="preference")
        self.assertEqual(item["source"], "user_explicit")
        self.assertEqual(len(self.store.active()), 1)

    def test_duplicate_text_not_stored_twice(self):
        self.store.add("住在上海")
        self.store.add("住在上海")
        self.assertEqual(len(self.store.all()), 1)

    def test_inferred_is_pending_until_confirmed(self):
        self.store.add("可能在学日语", kind="fact", source="inferred", status="pending")
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(len(self.store.active()), 0)

        confirmed = self.store.confirm(self.store.pending()[0]["id"])
        self.assertEqual(confirmed["status"], "active")
        self.assertEqual(len(self.store.active()), 1)

    def test_extracted_memories_are_never_auto_active(self):
        # 抽取出来的一律待确认——哪怕他亲口说过，也要他点头。
        # explicit 只用来在界面上标「这句你确实说过」，不代表可以跳过确认。
        item = self.store.add("在做嵌入式", source="inferred", status="pending", explicit=True)
        self.assertEqual(item["status"], "pending")
        self.assertTrue(item["explicit"])
        self.assertEqual(self.store.active(), [])
        self.assertNotIn("在做嵌入式", self.store.prompt_block())

    def test_explicit_flag_survives_reload(self):
        self.store.add("养了只猫叫咪咪", source="inferred", status="pending", explicit=True)
        reopened = MemoryStore(self.path)
        self.assertTrue(reopened.pending()[0]["explicit"])

    def test_forget_really_deletes(self):
        item = self.store.add("临时的事")
        self.assertTrue(self.store.forget(item["id"]))
        self.assertEqual(self.store.all(), [])

    def test_rejected_candidate_does_not_come_back(self):
        # 他说"不要"，她就不该下一轮又抽出同一条再问一遍
        item = self.store.add("喜欢熬夜", source="inferred", status="pending")
        self.store.forget(item["id"])
        again = self.store.add("喜欢熬夜", source="inferred", status="pending")
        self.assertIsNone(again)
        self.assertEqual(self.store.pending(), [])

    def test_rejected_list_survives_reload(self):
        item = self.store.add("喜欢熬夜", source="inferred", status="pending")
        self.store.forget(item["id"])
        reopened = MemoryStore(self.path)
        self.assertIsNone(reopened.add("喜欢熬夜", source="inferred", status="pending"))

    def test_forgetting_a_real_memory_does_not_block_it_forever(self):
        # 正式记忆删掉就是删掉；他以后再提一次，那是新的对话，该重新记得
        item = self.store.add("住在上海")
        self.store.forget(item["id"])
        again = self.store.add("住在上海", source="inferred", status="pending")
        self.assertIsNotNone(again)

    def test_direct_instruction_skips_the_rejected_list(self):
        # 他自己下命令"记住：我不喝咖啡"，就算之前否过一次也照记
        item = self.store.add("不喝咖啡", source="inferred", status="pending")
        self.store.forget(item["id"])
        direct = self.store.add("不喝咖啡", source="user_explicit", status="active")
        self.assertIsNotNone(direct)
        self.assertEqual(direct["status"], "active")

    def test_prompt_block_marks_guesses(self):
        self.store.add("主人叫小林")
        self.store.add("大概在准备考研", source="inferred", status="pending")
        self.store.confirm(self.store.pending()[0]["id"])
        block = self.store.prompt_block()
        self.assertIn("主人叫小林", block)

    def test_pending_never_enters_prompt(self):
        self.store.add("这条还没确认", source="inferred", status="pending")
        self.assertNotIn("这条还没确认", self.store.prompt_block())

    def test_empty_prompt_block_is_honest(self):
        self.assertIn("还没", self.store.prompt_block())

    def test_legacy_format_is_migrated(self):
        self.path.write_text(json.dumps([{"text": "旧格式的一条记忆"}], ensure_ascii=False), encoding="utf-8")
        store = MemoryStore(self.path)
        item = store.active()[0]
        self.assertEqual(item["text"], "旧格式的一条记忆")
        self.assertEqual(item["status"], "active")
        self.assertTrue(item["id"].startswith("mem_"))

    def test_persistence_round_trip(self):
        self.store.add("喜欢清淡口味")
        reopened = MemoryStore(self.path)
        self.assertEqual(reopened.active()[0]["text"], "喜欢清淡口味")

    # ---------- 排序：超过 40 条以后，新记忆不能饿死 ----------

    def test_new_memory_enters_prompt_after_many_old_ones(self):
        # 老实现按插入序取前 40 条，第 41 条以后的新记忆永远进不了提示词。
        old_stamp = (datetime.now(LOCAL_TZ) - timedelta(days=30)).isoformat(timespec="seconds")
        items = [{
            "id": f"mem_old{i}", "text": f"很久以前的事{i}", "kind": "fact",
            "source": "user_explicit", "status": "active", "created_at": old_stamp,
            "last_used": None, "pinned": False, "explicit": True, "importance": 3,
        } for i in range(60)]
        self.path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        store = MemoryStore(self.path)
        store.add("我对花生过敏")
        block = store.prompt_block("晚上想点外卖")
        self.assertIn("花生", block)
        self.assertLessEqual(len(block.splitlines()), 15)

    def test_used_memory_gets_touched(self):
        item = self.store.add("我住在上海")
        self.store.prompt_block("上海天气怎么样")
        self.assertIsNotNone(self.store.find(item["id"])["last_used"])

    def test_relevant_memory_beats_newer_noise(self):
        target = self.store.add("我对花生过敏")
        self.store.add("昨天看了个电影")
        block = self.store.prompt_block("晚饭有花生吗")
        self.assertIn("花生", block)
        self.assertIsNotNone(self.store.find(target["id"])["last_used"])

    def test_similar_active_finds_the_old_fact(self):
        # 字面完全不重合（我搬到北京了 / 主人住在上海），也要能把旧事实找出来，
        # 否则模型没机会判断"这是在更新它"。
        item = self.store.add("主人住在上海")
        self.store.add("我喜欢喝美式")
        ids = [row["id"] for row in self.store.similar_active("我搬到北京了")]
        self.assertIn(item["id"], ids)

    # ---------- 替换与出处 ----------

    def test_confirm_supersedes_old_fact(self):
        old = self.store.add("主人住在上海")
        new = self.store.add("主人搬到北京了", source="inferred", status="pending",
                             supersedes=[old["id"]])
        self.store.confirm(new["id"])
        self.assertEqual(self.store.find(old["id"])["status"], "superseded")
        self.assertEqual([row["text"] for row in self.store.active()], ["主人搬到北京了"])
        self.assertEqual([row["id"] for row in self.store.superseded()], [old["id"]])
        block = self.store.prompt_block("我住哪")
        self.assertIn("北京", block)
        self.assertNotIn("上海", block)

    def test_confirm_keeps_the_inferred_source(self):
        # 「她猜的、你批准了」和「你亲口说的」是两种东西，不能洗成一种。
        item = self.store.add("可能在学日语", source="inferred", status="pending")
        confirmed = self.store.confirm(item["id"])
        self.assertEqual(confirmed["source"], "inferred")
        self.assertIsNotNone(confirmed["approved_at"])

    def test_same_fact_can_come_back_after_supersede(self):
        # 搬走又搬回来，是新的断言：不能因为有一条已过期的同文记忆就吞掉。
        old = self.store.add("主人住在上海")
        new = self.store.add("主人搬到北京了", source="inferred", status="pending",
                             supersedes=[old["id"]])
        self.store.confirm(new["id"])
        again = self.store.add("主人住在上海", source="inferred", status="pending")
        self.assertIsNotNone(again)
        self.assertEqual(again["status"], "pending")
        self.assertNotEqual(again["id"], old["id"])
        self.assertEqual(len(self.store.pending()), 1)

    def test_direct_command_can_reactivate_a_superseded_fact(self):
        old = self.store.add("主人住在上海")
        new = self.store.add("主人搬到北京了", source="inferred", status="pending",
                             supersedes=[old["id"]])
        self.store.confirm(new["id"])
        direct = self.store.add("主人住在上海", source="user_explicit", status="active")
        self.assertEqual(direct["status"], "active")
        self.assertIsNone(direct["valid_to"])

    # ---------- 偏好数据 ----------

    def test_confirm_and_reject_are_logged_as_preferences(self):
        accepted = self.store.add("在做嵌入式", source="inferred", status="pending")
        rejected = self.store.add("可能喜欢早起", source="inferred", status="pending")
        self.store.confirm(accepted["id"])
        self.store.forget(rejected["id"])

        lines = [json.loads(line) for line in
                 self.store.prefs_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["decision"] for row in lines], ["accept", "reject"])
        self.assertEqual(lines[0]["text"], "在做嵌入式")

    def test_forgetting_a_real_memory_is_not_a_preference(self):
        item = self.store.add("住在上海")
        self.store.forget(item["id"])
        self.assertFalse(self.store.prefs_path.exists())

    # ---------- journal 与撤销 ----------

    def test_journal_records_lifecycle(self):
        item = self.store.add("住在上海")
        self.store.confirm(item["id"])
        self.store.forget(item["id"])
        events = [json.loads(line)["event"] for line in
                  self.store.journal_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(events, ["add", "confirm", "forget"])

    def test_forget_can_be_undone_within_a_week(self):
        item = self.store.add("临时的事")
        self.store.forget(item["id"])
        self.assertEqual([row["id"] for row in self.store.forgotten()], [item["id"]])

        restored = self.store.restore(item["id"])
        self.assertEqual(restored["text"], "临时的事")
        self.assertEqual([row["id"] for row in self.store.active()], [item["id"]])
        self.assertEqual(self.store.forgotten(), [])

    def test_restoring_a_rejected_candidate_unblocks_it(self):
        item = self.store.add("喜欢熬夜", source="inferred", status="pending")
        self.store.forget(item["id"])
        self.store.restore(item["id"])
        self.assertEqual(len(self.store.pending()), 1)
        self.assertIsNotNone(self.store.add("喜欢熬夜", source="inferred", status="pending"))

    def test_forgotten_window_expires(self):
        item = self.store.add("很久以前删的")
        self.store.forget(item["id"])
        old = (datetime.now(LOCAL_TZ) - timedelta(days=8)).isoformat(timespec="seconds")
        records = [json.loads(line) for line in
                   self.store.journal_path.read_text(encoding="utf-8").splitlines()]
        for record in records:
            if record["event"] == "forget":
                record["ts"] = old
        self.store.journal_path.write_text(
            "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
            encoding="utf-8")
        self.assertEqual(self.store.forgotten(), [])

    def test_export_includes_memories_and_rejected(self):
        self.store.add("住在上海")
        item = self.store.add("喜欢熬夜", source="inferred", status="pending")
        self.store.forget(item["id"])
        data = self.store.export_data()
        self.assertEqual([row["text"] for row in data["memories"]], ["住在上海"])
        self.assertIn("喜欢熬夜", data["rejected"])

    # ---------- 搜索外泄防护 ----------


    def test_contains_snippet_finds_memory_text_in_a_query(self):
        self.store.add("我的身份证号是 310101199001011234")
        hit = self.store.contains_snippet("帮我搜 310101199001011234 是什么")
        self.assertIsNotNone(hit)
        self.assertIsNone(self.store.contains_snippet("今天北京天气怎么样"))



    def test_evidence_survives(self):
        item = self.store.add("不喝咖啡", source="inferred", status="pending",
                              evidence={"session": "pet", "ts": "2026-11-17T22:03:00+08:00",
                                        "quote": "我不喝咖啡"})
        reopened = MemoryStore(self.path)
        self.assertEqual(reopened.find(item["id"])["evidence"]["quote"], "我不喝咖啡")


class ConversationLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "conversations.jsonl"
        self.log = ConversationLog(self.path, max_age_hours=6)

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_and_recent(self):
        self.log.append("user", "在吗")
        self.log.append("assistant", "在")
        recent = self.log.recent(limit=10)
        self.assertEqual([m["role"] for m in recent], ["user", "assistant"])
        self.assertEqual(recent[0]["content"], "在吗")

    def test_recent_respects_limit(self):
        for i in range(30):
            self.log.append("user", f"第{i}句")
        self.assertEqual(len(self.log.recent(limit=5)), 5)

    def test_old_turns_are_not_reinjected(self):
        stale = (datetime.now(LOCAL_TZ) - timedelta(hours=20)).isoformat(timespec="seconds")
        line = json.dumps({"ts": stale, "session": "default", "role": "user", "content": "昨晚的倾诉"})
        self.path.write_text(line + "\n", encoding="utf-8")
        self.log.append("user", "今天的新问题")
        contents = [m["content"] for m in self.log.recent(limit=10)]
        self.assertNotIn("昨晚的倾诉", contents)
        self.assertIn("今天的新问题", contents)

    def test_other_sessions_are_isolated(self):
        self.log.append("user", "网页里说的", session="web")
        self.log.append("user", "桌宠里说的", session="pet")
        contents = [m["content"] for m in self.log.recent(limit=10, session="web")]
        self.assertEqual(contents, ["网页里说的"])

    def test_stats(self):
        self.log.append("user", "一")
        self.assertEqual(self.log.stats()["turns"], 1)


class ExtractionTests(unittest.TestCase):
    def test_parses_json_array(self):
        fake = lambda prompt: '[{"text": "主人叫小林", "kind": "fact"}]'
        got = extract_candidates(fake, "我叫小林", "记住了")
        self.assertEqual(got[0]["text"], "主人叫小林")

    def test_tolerates_chatter_around_json(self):
        fake = lambda prompt: '好的，这是结果：\n[{"text": "讨厌香菜"}]\n希望有用。'
        got = extract_candidates(fake, "我不喝咖啡", "记下了")
        self.assertEqual(got[0]["text"], "讨厌香菜")

    def test_empty_array(self):
        self.assertEqual(extract_candidates(lambda p: "[]", "你好", "在"), [])

    def test_garbage_returns_nothing(self):
        self.assertEqual(extract_candidates(lambda p: "我不知道", "你好", "在"), [])

    def test_caps_item_count(self):
        fake = lambda prompt: json.dumps([{"text": f"事实{i}"} for i in range(10)])
        self.assertEqual(len(extract_candidates(fake, "a", "b", max_items=3)), 3)

    def test_does_not_echo_the_instruction_back(self):
        # 实测过的坑：提示词里写了示例事实，小模型会把示例当成对话内容输出，
        # 于是每次聊天都凭空多出两条假记忆。提示词现在不再写示例，
        # 这里再验一道兜底过滤。
        from memory import EXTRACT_PROMPT

        sample = " ".join(EXTRACT_PROMPT.split())[40:70]
        fake = lambda prompt: json.dumps([{"text": sample}, {"text": "真的说过的事"}])
        got = extract_candidates(fake, "随便说说", "嗯")
        texts = [item["text"] for item in got]
        self.assertIn("真的说过的事", texts)
        self.assertNotIn(sample, texts)

    def test_no_example_facts_in_the_prompt(self):
        # 提示词里不许再出现具体的示例事实——它们会被当成真事
        from memory import EXTRACT_PROMPT

        for leak in ("咪咪", "考研", "小林", "香菜", "三楼会议室"):
            self.assertNotIn(leak, EXTRACT_PROMPT, f"提示词里还有示例事实：{leak}")

    def test_explicit_flag_is_parsed(self):
        fake = lambda prompt: json.dumps([{"text": "在做嵌入式", "explicit": True},
                                          {"text": "可能喜欢早起", "explicit": False}])
        got = extract_candidates(fake, "a", "b")
        self.assertTrue(got[0]["explicit"])
        self.assertFalse(got[1]["explicit"])



class StyleTests(unittest.TestCase):
    """口味：他抱怨一次，她记下来，以后照这个说。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "m.json")

    def tearDown(self):
        self.tmp.cleanup()

    def _model(self, payload):
        def call(prompt):
            self.last_prompt = prompt
            return payload
        return call

    def test_style_candidate_is_kept(self):
        payload = '[{"text": "希望说话简短", "kind": "style", "explicit": true}]'
        got = extract_candidates(self._model(payload), "太长了", "好", existing=[])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["kind"], "style")

    def test_style_goes_to_pending_not_active(self):
        item = self.store.add("希望说话简短", kind="style", status="pending")
        self.assertEqual(item["status"], "pending")
        self.assertEqual(self.store.style_block(), "")

    def test_style_block_after_confirm(self):
        item = self.store.add("希望说话简短", kind="style", status="pending")
        self.store.confirm(item["id"])
        self.assertIn("希望说话简短", self.store.style_block())

    def test_style_is_kept_out_of_fact_block(self):
        item = self.store.add("希望说话简短", kind="style")
        self.store.confirm(item["id"])
        self.store.add("在准备考研", kind="event")
        facts = self.store.prompt_block()
        self.assertNotIn("希望说话简短", facts)
        self.assertIn("考研", facts)

    def test_style_block_empty_without_styles(self):
        self.store.add("在准备考研", kind="event")
        self.assertEqual(self.store.style_block(), "")

    def test_verbatim_copy_of_prompt_is_still_rejected(self):
        # 提示词里出现过的话不能当候选——模型抄示例是老毛病
        payload = '[{"text": "不要照抄他的原话", "kind": "style", "explicit": true}]'
        got = extract_candidates(self._model(payload), "太长了", "好", existing=[])
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
