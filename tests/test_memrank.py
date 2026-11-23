"""挑记忆这件事必须能被钉死，否则调参就是盲调。"""

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import memrank

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=TZ)


def row(id_, text, **kwargs):
    item = {
        "id": id_, "text": text, "kind": "fact", "source": "user_explicit",
        "status": "active", "importance": 3, "heat": 1.0, "pinned": False,
        "created_at": (NOW - timedelta(days=1)).isoformat(timespec="seconds"),
        "last_used": None,
    }
    item.update(kwargs)
    return item


class GramTests(unittest.TestCase):
    def test_two_gram_catches_chinese_substring(self):
        self.assertTrue(memrank.grams("我住在上海") & memrank.grams("上海"))

    def test_overlap_is_bounded(self):
        self.assertLessEqual(memrank.overlap(memrank.grams("上海"), memrank.grams("上海")), 1.0)

    def test_empty_is_zero_not_crash(self):
        self.assertEqual(memrank.overlap(set(), memrank.grams("上海")), 0.0)


class ScoreTests(unittest.TestCase):
    def test_lexical_hit_beats_unrelated(self):
        hit, _ = memrank.select([row("a", "主人住在上海"), row("b", "喜欢喝咖啡")],
                                "我住哪", now=NOW)
        self.assertEqual(hit[0]["id"], "a")

    def test_pinned_always_in_even_if_irrelevant(self):
        rows = [row("a", "完全无关的一句话", pinned=True), row("b", "主人住在上海")]
        picked, _ = memrank.select(rows, "上海", now=NOW)
        self.assertIn("a", [item["id"] for item in picked])

    def test_char_budget_truncates(self):
        rows = [row(f"m{i}", "很长很长的一句话" * 10) for i in range(5)]
        picked, _ = memrank.select(rows, "", now=NOW, chars=120)
        self.assertLessEqual(sum(len(item["text"]) for item in picked), 120)

    def test_limit_is_respected(self):
        rows = [row(f"m{i}", f"第{i}条") for i in range(30)]
        picked, _ = memrank.select(rows, "", now=NOW, limit=12)
        self.assertLessEqual(len(picked), 12)

    def test_fallback_capped_at_three(self):
        # 一条都不相关时不能把整个库倒进提示词
        rows = [row(f"m{i}", f"完全不相干的第{i}句") for i in range(10)]
        picked, _ = memrank.select(rows, "量子色动力学", now=NOW)
        self.assertLessEqual(len(picked), 3)

    def test_trace_explains_every_candidate(self):
        rows = [row("a", "主人住在上海"), row("b", "喜欢喝咖啡")]
        _, trace = memrank.select(rows, "上海", now=NOW, with_trace=True)
        self.assertEqual(len(trace), 2)
        for item in trace:
            self.assertIn(item["why"], ("字面命中", "语义命中", "近期", "重要", "最近用过", "填充", "置顶"))
            self.assertIn("score", item)


class SemanticTests(unittest.TestCase):
    def test_vector_hit_beats_zero_overlap(self):
        # 字面零重合，只有向量能救——这正是换掉纯 n-gram 的理由
        rows = [
            dict(row("a", "主人住在上海"), vec=[1.0, 0.0]),
            dict(row("b", "喜欢喝咖啡"), vec=[0.0, 1.0]),
        ]
        picked, trace = memrank.select(rows, "我搬到哪座城市了", now=NOW,
                                       query_vec=[1.0, 0.0], with_trace=True)
        self.assertEqual(picked[0]["id"], "a")
        self.assertGreater(trace[0]["sem"], 0.9)

    def test_missing_vector_is_not_an_error(self):
        rows = [row("a", "主人住在上海")]
        picked, _ = memrank.select(rows, "上海", now=NOW, query_vec=None)
        self.assertEqual(len(picked), 1)


class MmrTests(unittest.TestCase):
    def test_near_duplicates_do_not_fill_the_budget(self):
        rows = [row("a", "主人住在上海"), row("b", "主人住在上海。"), row("c", "喜欢喝咖啡")]
        picked, _ = memrank.select(rows, "上海", now=NOW, limit=2, tau=0.05)
        ids = [item["id"] for item in picked]
        self.assertEqual(len(ids), 2)
        self.assertNotEqual(set(ids), {"a", "b"})


class HeatTests(unittest.TestCase):
    def test_cold_memory_ranks_lower(self):
        warm = row("warm", "主人住在上海")
        cold = row("cold", "主人住在上海", heat=0.0)
        picked, _ = memrank.select([cold, warm], "上海", now=NOW, limit=1)
        self.assertEqual(picked[0]["id"], "warm")


if __name__ == "__main__":
    unittest.main()
