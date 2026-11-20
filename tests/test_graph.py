import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import graph
from graph import GraphStore

CANNED = json.dumps({
    "entities": [
        {"name": "主人", "type": "person"},
        {"name": "课程报告", "type": "project"},
        {"name": "咪咪", "type": "pet"},
    ],
    "relations": [
        {"from": "主人", "rel": "正在做", "to": "课程报告"},
        {"from": "主人", "rel": "养了", "to": "咪咪"},
        {"from": "主人", "rel": "正在做", "to": "课程报告"},   # 重复，应被去掉
    ],
}, ensure_ascii=False)


class GraphTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "graph.json"
        self.prompt_seen = []

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, answer=CANNED):
        def extractor(prompt):
            self.prompt_seen.append(prompt)
            return answer
        return GraphStore(self.path, extractor=extractor)

    def memories(self, *texts):
        return [{"text": t, "kind": "fact"} for t in texts]

    def test_builds_entities_and_relations(self):
        store = self.make()
        store.rebuild(self.memories("在赶一个课程报告", "养了只猫叫咪咪"))
        summary = store.summary()
        self.assertEqual(summary["entities"], 3)
        self.assertEqual(summary["relations"], 2, "重复的关系应该合并")

    def test_prompt_block_reads_like_sentences(self):
        store = self.make()
        store.rebuild(self.memories("在赶一个课程报告", "养了只猫叫咪咪"))
        block = store.prompt_block()
        self.assertIn("主人 正在做 课程报告", block)
        self.assertIn("主人 养了 咪咪", block)

    def test_empty_memories_clear_the_graph(self):
        store = self.make()
        store.rebuild(self.memories("在赶一个课程报告"))
        self.assertGreater(store.summary()["entities"], 0)
        store.rebuild([])
        self.assertEqual(store.summary()["entities"], 0)
        self.assertEqual(store.prompt_block(), "")

    def test_no_extractor_is_safe(self):
        store = GraphStore(self.path)
        store.rebuild(self.memories("随便"))
        self.assertEqual(store.summary()["entities"], 0)

    def test_garbage_answer_does_not_crash(self):
        store = self.make(answer="我觉得这些东西很重要，但我不想输出 JSON。")
        store.rebuild(self.memories("在赶一个课程报告"))
        self.assertEqual(store.summary()["entities"], 0)

    def test_partial_json_is_tolerated(self):
        store = self.make(answer='{"entities": [{"name": "主人"}, {"name": "咪咪", "type": "pet"}]}')
        store.rebuild(self.memories("养了只猫叫咪咪"))
        self.assertEqual(store.summary()["entities"], 2)
        self.assertEqual(store.summary()["relations"], 0)

    def test_relations_pointing_at_unknown_entities_get_added(self):
        store = self.make(answer=json.dumps({
            "entities": [{"name": "主人", "type": "person"}],
            "relations": [{"from": "主人", "rel": "认识", "to": "老王"}],
        }, ensure_ascii=False))
        store.rebuild(self.memories("认识老王"))
        self.assertIn("老王", store.data["entities"])

    def test_bad_entity_type_falls_back(self):
        store = self.make(answer=json.dumps({
            "entities": [{"name": "奇怪的东西", "type": "外星人"}],
            "relations": [],
        }, ensure_ascii=False))
        store.rebuild(self.memories("有个奇怪的东西"))
        self.assertEqual(store.data["entities"]["奇怪的东西"]["type"], "thing")

    def test_persists_and_reloads(self):
        store = self.make()
        store.rebuild(self.memories("在赶一个课程报告"))
        again = GraphStore(self.path)
        self.assertEqual(again.summary()["relations"], 2)
        self.assertIn("课程报告", again.prompt_block())

    def test_corrupt_file_is_ignored(self):
        self.path.write_text("{ broken", encoding="utf-8")
        store = GraphStore(self.path)
        self.assertEqual(store.summary()["entities"], 0)

    def test_prompt_includes_real_facts(self):
        store = self.make()
        store.rebuild(self.memories("在赶一个课程报告"))
        self.assertIn("在赶一个课程报告", self.prompt_seen[0])

    def test_prompt_block_respects_limit(self):
        many = json.dumps({
            "entities": [{"name": f"实体{i}", "type": "thing"} for i in range(50)],
            "relations": [{"from": f"实体{i}", "rel": "连着", "to": f"实体{i+1}"} for i in range(49)],
        }, ensure_ascii=False)
        store = self.make(answer=many)
        store.rebuild(self.memories("一堆东西"))
        self.assertLessEqual(len(store.prompt_block(limit=5).splitlines()), 6)


if __name__ == "__main__":
    unittest.main()
