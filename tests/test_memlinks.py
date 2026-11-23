"""边和退化闸：图不能飘，合并不能丢东西，矛盾不能自动裁决。"""

import sqlite3
from contextlib import closing
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import memdb
import memlinks

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=TZ)


def iso(moment):
    return moment.isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = memdb.MemDB(self.dir / "memories.json")
        self.store = memlinks.EdgeStore(self.db.db_path)

    def tearDown(self):
        self.tmp.cleanup()


class EdgeTests(Base):
    def test_rule_edges_flag_near_duplicates(self):
        a = self.db.add("主人现在住在上海市静安区")
        b = self.db.add("主人现在住在上海市静安区了")
        edges = memlinks.rule_edges(self.db.find(b["id"]), [self.db.find(a["id"])])
        self.assertTrue(any(edge["type"] == "same_as" for edge in edges))

    def test_supersedes_becomes_an_edge(self):
        old = self.db.add("主人住在上海")
        new = self.db.add("主人搬到北京了", supersedes=[old["id"]])
        edges = memlinks.rule_edges(self.db.find(new["id"]), [])
        self.assertEqual(edges[0]["type"], "supersedes")
        self.assertEqual(edges[0]["src"], old["id"])

    def test_link_new_persists_with_reason(self):
        a = self.db.add("主人现在住在上海市静安区")
        b = self.db.add("主人现在住在上海市静安区了")
        saved = memlinks.link_new(self.store, self.db, self.db.find(b["id"]))
        self.assertTrue(saved)
        for edge in saved:
            self.assertTrue(edge["why"])
            self.assertTrue(edge["evidence_ids"])

    def test_forgetting_a_memory_drops_its_edges(self):
        a = self.db.add("主人现在住在上海市静安区")
        b = self.db.add("主人现在住在上海市静安区了")
        memlinks.link_new(self.store, self.db, self.db.find(b["id"]))
        self.assertGreater(self.store.count(), 0)
        self.db.forget(b["id"])
        removed = self.store.drop_for(b["id"])
        self.assertGreater(removed, 0)
        self.assertEqual(self.store.count(), 0)

    def test_prune_dangling(self):
        a = self.db.add("甲")
        b = self.db.add("乙")
        self.store.add(a["id"], b["id"], "supports", why="测试")
        self.db.forget(b["id"])
        self.assertEqual(self.store.prune_dangling({a["id"]}), 1)

    def test_neighborhood_walks_two_hops(self):
        ids = [self.db.add(f"记忆{i}")["id"] for i in range(3)]
        self.store.add(ids[0], ids[1], "supports", why="甲")
        self.store.add(ids[1], ids[2], "supports", why="乙")
        hood = self.store.neighbors(ids[0], depth=2)
        self.assertEqual(set(hood["ids"]), set(ids))


class ConsolidateTests(Base):
    def test_merge_keeps_one_and_archives_the_other(self):
        a = self.db.add("主人现在住在上海市静安区", importance=4)
        b = self.db.add("主人现在住在上海市静安区了", importance=2)
        merged = memlinks.merge_duplicates(self.db, self.store, threshold=0.8)
        self.assertEqual(len(merged), 1)
        kept = self.db.find(merged[0]["kept"])
        dropped = self.db.find(merged[0]["dropped"])
        self.assertEqual(kept["status"], "active")
        self.assertEqual(dropped["status"], "archived")
        self.assertEqual((dropped.get("meta") or {}).get("merged_into"), kept["id"])

    def test_heat_decays_once_per_day(self):
        self.db.add("主人住在上海")
        first = memlinks.decay_heat(self.db, now=NOW)
        self.assertLess(first, 1.0)
        heat = self.db.active()[0]["heat"]
        second = memlinks.decay_heat(self.db, now=NOW)
        self.assertEqual(second, 0)
        self.assertAlmostEqual(heat, self.db.active()[0]["heat"], places=6)

    def test_pinned_never_cools(self):
        item = self.db.add("主人住在上海", pinned=True)
        memlinks.decay_heat(self.db, now=NOW)
        self.assertEqual(self.db.find(item["id"])["heat"], 1.0)

    def test_cold_and_old_gets_archived(self):
        item = self.db.add("很久以前的一件事")
        with closing(sqlite3.connect(self.db.db_path)) as conn:
            conn.execute("UPDATE memories SET heat=0.1, last_used=? WHERE id=?",
                         (iso(NOW - timedelta(days=400)), item["id"]))
            conn.commit()
        archived = memlinks.archive_cold(self.db, now=NOW)
        self.assertEqual(archived, [item["id"]])
        self.assertEqual(self.db.find(item["id"])["status"], "archived")

    def test_conflicts_are_reported_not_resolved(self):
        a = self.db.add("每天跑步五公里")
        b = self.db.add("膝盖疼不能跑步了")
        self.store.add(a["id"], b["id"], "contradicts", weight=0.8, why="同一个人的运动习惯，前后冲突")
        found = memlinks.conflicts(self.db, self.store)
        self.assertEqual(len(found), 1)
        self.assertEqual(self.db.find(a["id"])["status"], "active")
        self.assertEqual(self.db.find(b["id"])["status"], "active")

    def test_consolidate_reports_everything(self):
        self.db.add("主人现在住在上海市静安区", importance=4)
        self.db.add("主人现在住在上海市静安区了", importance=2)
        report = memlinks.consolidate(self.db, self.store, now=NOW)
        self.assertIn("merged", report)
        self.assertIn("conflicts", report)
        self.assertGreaterEqual(report["edges"], 0)


if __name__ == "__main__":
    unittest.main()
