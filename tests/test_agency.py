"""能动性的最小证据：目标能核对、差值不误报、动作不越级、心跳不停。"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import agency
import gap
import goals
import predictions
import ticker

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=TZ)


def iso(moment):
    return moment.isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db_path = self.dir / "zero.db"

    def tearDown(self):
        self.tmp.cleanup()


class GoalTests(Base):
    def test_file_predicate_is_machine_checkable(self):
        target = self.dir / "报告.md"
        store = goals.GoalStore(self.db_path)
        goal = store.add("交报告", success={"kind": "file_exists", "path": str(target),
                                          "min_bytes": 4})
        observed, why = goals.evaluate(goal)
        self.assertFalse(observed)
        target.write_text("写完了", encoding="utf-8")
        observed, why = goals.evaluate(store.find(goal["id"]))
        self.assertTrue(observed)

    def test_checkin_is_unknown_not_false(self):
        store = goals.GoalStore(self.db_path)
        goal = store.add("考研准备得怎么样", success={"kind": "checkin"})
        observed, why = goals.evaluate(goal)
        self.assertIsNone(observed)          # 判不了 ≠ 没进展
        self.assertIn("本人", why)

    def test_memory_predicate_uses_memory_texts(self):
        store = goals.GoalStore(self.db_path)
        goal = store.add("整理简历", success={"kind": "memory_matches", "text": "简历投出去了"})
        observed, _ = goals.evaluate(goal, memory_texts=["今天把简历投出去了"])
        self.assertTrue(observed)

    def test_self_goals_are_capped(self):
        store = goals.GoalStore(self.db_path)
        for index in range(goals.MAX_SELF_GOALS):
            self.assertIsNotNone(store.add(f"自定目标{index}", owner="self", source="maintenance"))
        self.assertIsNone(store.add("再多一个", owner="self", source="maintenance"))

    def test_progress_and_mute(self):
        store = goals.GoalStore(self.db_path)
        goal = store.add("交报告", deadline=iso(NOW + timedelta(days=3)))
        store.set_progress(goal["id"], 0.6, note="写完一半")
        self.assertAlmostEqual(store.find(goal["id"])["progress"], 0.6)
        store.mute(goal["id"], days=14)
        self.assertTrue(store.is_muted(store.find(goal["id"]), now=NOW))


class GapTests(Base):
    def _store(self):
        return goals.GoalStore(self.db_path)

    def test_deadline_rule_fires_when_behind(self):
        store = self._store()
        goal = store.add("交报告", deadline=iso(NOW + timedelta(days=2)),
                         importance=4, created_at=iso(NOW - timedelta(days=8)))
        engine = gap.GapEngine(self.dir / "gap.json")
        found = engine.evaluate([store.find(goal["id"])], now=NOW)
        self.assertEqual(len(found), 1)
        self.assertGreater(found[0]["severity"], 0.3)
        self.assertIn("期限", found[0]["reason"])

    def test_on_track_goal_produces_nothing(self):
        store = self._store()
        goal = store.add("交报告", deadline=iso(NOW + timedelta(days=2)),
                         created_at=iso(NOW - timedelta(days=8)))
        store.set_progress(goal["id"], 0.75)
        engine = gap.GapEngine(self.dir / "gap.json")
        self.assertEqual(engine.evaluate([store.find(goal["id"])], now=NOW), [])

    def test_same_goal_is_not_reported_twice(self):
        store = self._store()
        goal = store.add("交报告", deadline=iso(NOW + timedelta(days=2)),
                         created_at=iso(NOW - timedelta(days=8)))
        engine = gap.GapEngine(self.dir / "gap.json")
        self.assertEqual(len(engine.evaluate([store.find(goal["id"])], now=NOW)), 1)
        self.assertEqual(engine.evaluate([store.find(goal["id"])], now=NOW), [])

    def test_muted_goal_is_silent(self):
        store = self._store()
        goal = store.add("交报告", deadline=iso(NOW + timedelta(days=2)),
                         created_at=iso(NOW - timedelta(days=8)))
        store.mute(goal["id"], days=14)
        engine = gap.GapEngine(self.dir / "gap.json")
        self.assertEqual(engine.evaluate([store.find(goal["id"])], now=NOW), [])

    def test_completed_goal_is_not_a_gap(self):
        store = self._store()
        goal = store.add("交报告", deadline=iso(NOW + timedelta(days=2)),
                         created_at=iso(NOW - timedelta(days=8)))
        engine = gap.GapEngine(self.dir / "gap.json")
        found = engine.evaluate([store.find(goal["id"])], now=NOW,
                                evidence={goal["id"]: (True, "文件已存在")})
        self.assertEqual(found, [])

    def test_stagnation_without_deadline(self):
        store = self._store()
        goal = store.add("整理旧照片", created_at=iso(NOW - timedelta(days=10)))
        engine = gap.GapEngine(self.dir / "gap.json")
        found = engine.evaluate([store.find(goal["id"])], now=NOW)
        self.assertEqual(len(found), 1)
        self.assertIn("没有推进", found[0]["reason"])


class PredictionTests(Base):
    def test_factor_learns_overestimation(self):
        ledger = predictions.PredictionLedger(self.db_path)
        for index in range(10):
            item = ledger.record("user_interest", f"预测{index}", p=0.8,
                                 resolve_at=iso(NOW), subject_id="goal_1")
            ledger.resolve(item["id"], outcome=index < 4)
        self.assertAlmostEqual(ledger.factor("user_interest"), 0.5, places=2)
        summary = ledger.summary()["user_interest"]
        self.assertEqual(summary["hit"], 4)
        self.assertEqual(summary["miss"], 6)

    def test_small_sample_does_not_change_behavior(self):
        ledger = predictions.PredictionLedger(self.db_path)
        for index in range(3):
            item = ledger.record("timing", f"预测{index}", p=0.9, resolve_at=iso(NOW))
            ledger.resolve(item["id"], outcome=True)
        self.assertEqual(ledger.factor("timing"), 1.0)

    def test_error_kind_is_classified(self):
        ledger = predictions.PredictionLedger(self.db_path)
        item = ledger.record("outcome", "他会问简历", p=0.9, resolve_at=iso(NOW))
        resolved = ledger.resolve(item["id"], outcome=False)
        self.assertEqual(resolved["error_kind"], "over")

    def test_due_only(self):
        ledger = predictions.PredictionLedger(self.db_path)
        ledger.record("timing", "三天内会到期", p=0.7, resolve_at=iso(NOW - timedelta(days=1)))
        ledger.record("timing", "下周的事", p=0.7, resolve_at=iso(NOW + timedelta(days=7)))
        self.assertEqual(len(ledger.open_items(now=NOW, due_only=True)), 1)


class LadderTests(Base):
    def _engine(self, **kwargs):
        return agency.Agency(self.db_path, **kwargs)

    def test_low_severity_stays_silent(self):
        engine = self._engine(mode="act")
        picked = engine.choose({"severity": 0.1}, {"user_present": True})
        self.assertEqual(picked["level"], agency.A0)

    def test_observe_mode_never_speaks(self):
        engine = self._engine(mode="observe")
        picked = engine.choose({"severity": 0.9}, {"user_present": True})
        self.assertEqual(picked["level"], agency.A1)

    def test_act_mode_can_do_reversible_work(self):
        engine = self._engine(mode="act", whitelist=[str(self.dir)])
        picked = engine.choose({"severity": 0.9}, {
            "user_present": False, "has_plan": True,
            "reversible": True, "whitelist_ok": True,
        })
        self.assertEqual(picked["level"], agency.A4)

    def test_propose_mode_stops_at_three(self):
        engine = self._engine(mode="propose")
        picked = engine.choose({"severity": 0.9}, {
            "user_present": False, "has_plan": True,
            "reversible": True, "whitelist_ok": True,
        })
        self.assertEqual(picked["level"], agency.A3)

    def test_never_returns_a5(self):
        for mode in agency.MODES:
            engine = self._engine(mode=mode)
            for present in (True, False):
                picked = engine.choose({"severity": 1.0}, {
                    "user_present": present, "has_plan": True,
                    "reversible": True, "whitelist_ok": True,
                })
                self.assertNotEqual(picked["level"], agency.A5)

    def test_speak_budget_is_weekly(self):
        engine = self._engine(mode="act", speak_per_week=1)
        self.assertTrue(engine.can_speak(NOW))
        engine.spend_speak(NOW)
        self.assertFalse(engine.can_speak(NOW))
        picked = engine.choose({"severity": 0.9}, {"user_present": True})
        self.assertNotEqual(picked["level"], agency.A2)

    def test_whitelist_blocks_outside_paths(self):
        engine = self._engine(whitelist=[str(self.dir / "safe")])
        self.assertFalse(engine.in_whitelist(self.dir / "outside.txt"))
        self.assertTrue(engine.in_whitelist(self.dir / "safe" / "a.txt"))

    def test_every_decision_is_logged(self):
        engine = self._engine(mode="act")
        picked = engine.choose({"severity": 0.9}, {"user_present": True})
        engine.log(picked["level"], "goal_1", picked["reason"])
        recent = engine.recent_log()
        self.assertEqual(recent[0]["level"], picked["level"])

    def test_off_is_a_hard_stop(self):
        engine = self._engine(mode="off")
        engine.set_mode("off")
        picked = engine.choose({"severity": 1.0}, {"user_present": True, "has_plan": True})
        self.assertEqual(picked["level"], agency.A0)


class TickerTests(Base):
    def test_exception_does_not_kill_the_heartbeat(self):
        def boom(now, catch_up=False):
            raise RuntimeError("模型没了")

        beat = ticker.Ticker(boom, state_path=self.dir / "tick.json")
        result = beat.tick_once(now=NOW)
        self.assertIn("error", result)
        self.assertEqual(beat.errors, 1)
        self.assertEqual(beat.ticks, 1)

    def test_long_sleep_only_catches_up_once(self):
        seen = []

        def record(now, catch_up=False):
            seen.append(catch_up)
            return {}

        beat = ticker.Ticker(record, state_path=self.dir / "tick.json")
        beat.tick_once(now=NOW - timedelta(hours=9))
        beat.tick_once(now=NOW)
        self.assertEqual(seen, [False, True])
        self.assertEqual(len(seen), 2)

    def test_state_survives_restart(self):
        beat = ticker.Ticker(lambda now, catch_up=False: {}, state_path=self.dir / "tick.json")
        beat.tick_once(now=NOW)
        again = ticker.Ticker(lambda now, catch_up=False: {}, state_path=self.dir / "tick.json")
        self.assertEqual(again.last_tick(), NOW)


if __name__ == "__main__":
    unittest.main()
