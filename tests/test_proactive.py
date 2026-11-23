import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import proactive
from proactive import LOCAL_TZ, ProactivePlanner


def at(year=2026, month=11, day=17, hour=9, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "proactive.json"
        self.planner = ProactivePlanner(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    # ---------- 早上 ----------

    def test_greets_once_in_the_morning(self):
        now = at(hour=8)
        self.assertEqual(self.planner.decide(now, event="panel_open"), proactive.KIND_MORNING)
        self.planner.record_spoken(proactive.KIND_MORNING, now)
        self.assertIsNone(self.planner.decide(now + timedelta(minutes=100), event="panel_open"))

    def test_no_morning_greeting_at_night(self):
        self.assertIsNone(self.planner.decide(at(hour=3, minute=30), event="panel_open"))

    def test_morning_only_when_he_opens_the_panel(self):
        # 心跳轮询不该触发早安——他根本不在
        self.assertIsNone(self.planner.decide(at(hour=8), event="tick"))

    def test_noon_is_not_morning(self):
        self.assertIsNone(self.planner.decide(at(hour=14), event="panel_open"))

    # ---------- 预算与间隔 ----------

    def test_daily_budget_is_enforced(self):
        planner = ProactivePlanner(self.path, budget=1)
        now = at(hour=8)
        self.assertEqual(planner.decide(now, event="panel_open"), proactive.KIND_MORNING)
        planner.record_spoken(proactive.KIND_MORNING, now)
        # 预算用完，今晚他还在坐着也不该再开口
        self.assertIsNone(planner.decide(now + timedelta(hours=5), event="tick", sit_minutes=300))

    def test_gap_between_two_lines(self):
        planner = ProactivePlanner(self.path, gap_minutes=90)
        now = at(hour=8)
        planner.record_spoken(proactive.KIND_MORNING, now)
        self.assertIsNone(planner.decide(now + timedelta(minutes=30), event="tick", sit_minutes=200))
        self.assertIsNotNone(planner.decide(now + timedelta(minutes=91), event="tick", sit_minutes=200))

    def test_budget_resets_next_day(self):
        planner = ProactivePlanner(self.path, budget=1)
        planner.record_spoken(proactive.KIND_MORNING, at(hour=8))
        tomorrow = at(day=18, hour=8)
        self.assertEqual(planner.decide(tomorrow, event="panel_open"), proactive.KIND_MORNING)

    # ---------- 不插嘴 ----------

    def test_never_interrupts_mid_conversation(self):
        # 主人刚说完话，就算坐了三小时也不许插嘴
        self.assertIsNone(self.planner.decide(at(hour=15), event="tick",
                                             sit_minutes=300, recent_user_minutes=1))

    def test_speaks_after_conversation_cools_down(self):
        self.assertEqual(self.planner.decide(at(hour=15), event="tick",
                                             sit_minutes=100, recent_user_minutes=30),
                         proactive.KIND_SIT)

    # ---------- 久坐 ----------

    def test_sit_reminder_needs_real_duration(self):
        self.assertIsNone(self.planner.decide(at(hour=15), event="tick", sit_minutes=45))

    def test_sit_reminder_fires_once(self):
        now = at(hour=15)
        self.assertEqual(self.planner.decide(now, event="tick", sit_minutes=95), proactive.KIND_SIT)
        self.planner.record_spoken(proactive.KIND_SIT, now)
        self.assertIsNone(self.planner.decide(now + timedelta(minutes=200), event="tick",
                                              sit_minutes=300))

    # ---------- 夜深 ----------

    def test_late_night_only_once(self):
        now = at(hour=23, minute=40)
        self.assertEqual(self.planner.decide(now, event="tick"), proactive.KIND_LATE_NIGHT)
        self.planner.record_spoken(proactive.KIND_LATE_NIGHT, now)
        self.assertIsNone(self.planner.decide(now + timedelta(minutes=120), event="tick"))

    def test_quiet_hours_block_chatter(self):
        # 凌晨三点，除了"太晚了"之外什么都不说；而"太晚了"已经说过了
        planner = ProactivePlanner(self.path)
        now = at(hour=3)
        kind = planner.decide(now, event="tick", sit_minutes=300)
        self.assertIn(kind, (None, proactive.KIND_LATE_NIGHT))
        if kind:
            planner.record_spoken(kind, now)
        self.assertIsNone(planner.decide(now + timedelta(minutes=90), event="tick", sit_minutes=300))

    # ---------- 久别归来 ----------

    def test_return_after_a_long_absence(self):
        planner = ProactivePlanner(self.path)
        planner.record_open(at(day=15, hour=20))
        self.assertEqual(planner.decide(at(day=17, hour=20), event="panel_open"),
                         proactive.KIND_RETURN)

    def test_no_return_line_after_a_short_break(self):
        planner = ProactivePlanner(self.path)
        planner.record_open(at(hour=13))
        self.assertIsNone(planner.decide(at(hour=15), event="panel_open"))

    # ---------- 记账 ----------

    def test_state_survives_restart(self):
        self.planner.record_spoken(proactive.KIND_MORNING, at(hour=8))
        reopened = ProactivePlanner(self.path)
        self.assertEqual(reopened.snapshot()["count"], 1)

    def test_remaining_counts_down(self):
        self.assertEqual(self.planner.remaining(at(hour=8)), 3)
        self.planner.record_spoken(proactive.KIND_MORNING, at(hour=8))
        self.assertEqual(self.planner.remaining(at(hour=8)), 2)

    def test_corrupt_state_file_does_not_crash(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(self.planner.snapshot()["count"], 0)

    def test_disabled_budget_silences_everything(self):
        planner = ProactivePlanner(self.path, budget=0)
        self.assertIsNone(planner.decide(at(hour=8), event="panel_open"))


class CueTests(unittest.TestCase):
    def test_every_kind_has_a_label_and_a_cue(self):
        for kind in (proactive.KIND_MORNING, proactive.KIND_RETURN,
                     proactive.KIND_LATE_NIGHT, proactive.KIND_SIT):
            self.assertIn(kind, proactive.LABELS)
            self.assertIn(kind, proactive.CUES)

    def test_cues_insist_it_is_her_turn(self):
        for kind, cue in proactive.CUES.items():
            self.assertIn("主动开口", cue, kind)

    def test_sit_cue_demands_one_line_and_no_lecture(self):
        cue = proactive.CUES[proactive.KIND_SIT]
        self.assertIn("一句话", cue)
        self.assertIn("不要像健康科普", cue)



class NoticeTests(unittest.TestCase):
    """按事开口：提醒要到了、想起你说过的事。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.now = datetime(2026, 11, 20, 14, 0, tzinfo=proactive.LOCAL_TZ)

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, budget=3, gap=90):
        return proactive.ProactivePlanner(self.path, budget=budget, gap_minutes=gap)

    def test_notice_wins_over_everything(self):
        p = self.make()
        kind = p.decide(self.now, event="tick", notice={"id": "r1", "minutes": 12})
        self.assertEqual(kind, proactive.KIND_NOTICE)

    def test_notice_is_not_repeated_twice(self):
        p = self.make()
        self.assertEqual(p.decide(self.now, event="tick",
                                  notice={"id": "r1", "minutes": 12}), proactive.KIND_NOTICE)
        p.record_spoken(proactive.KIND_NOTICE, self.now, key="notice:r1")
        # 隔得够久，次数也够，但同一件事不再提
        later = self.now + timedelta(minutes=120)
        self.assertIsNone(p.decide(later, event="tick", notice={"id": "r1", "minutes": 5}))

    def test_two_different_notices_are_independent(self):
        p = self.make(budget=5)
        p.record_spoken(proactive.KIND_NOTICE, self.now, key="notice:r1")
        later = self.now + timedelta(minutes=120)
        self.assertEqual(p.decide(later, event="tick",
                                  notice={"id": "r2", "minutes": 8}), proactive.KIND_NOTICE)

    def test_notice_is_allowed_in_quiet_hours(self):
        # 他自己设的事，半夜也照样说
        late = datetime(2026, 11, 20, 23, 40, tzinfo=proactive.LOCAL_TZ)
        p = self.make()
        self.assertEqual(p.decide(late, event="tick",
                                  notice={"id": "r1", "minutes": 10}), proactive.KIND_NOTICE)

    def test_notice_still_respects_recent_user(self):
        p = self.make()
        self.assertIsNone(p.decide(self.now, event="tick", recent_user_minutes=1,
                                   notice={"id": "r1", "minutes": 10}))

    def test_notice_still_respects_budget(self):
        p = self.make(budget=1)
        p.record_spoken(proactive.KIND_MORNING, self.now)
        later = self.now + timedelta(minutes=120)
        self.assertIsNone(p.decide(later, event="tick", notice={"id": "r1", "minutes": 10}))

    def test_callback_only_when_panel_opens(self):
        p = self.make()
        later = self.now + timedelta(minutes=120)
        self.assertIsNone(p.decide(later, event="tick", callback="在准备考研"))
        self.assertEqual(p.decide(later, event="panel_open", callback="在准备考研"),
                         proactive.KIND_CALLBACK)

    def test_callback_once_per_day(self):
        p = self.make(budget=5, gap=1)
        self.assertEqual(p.decide(self.now, event="panel_open", callback="在准备考研"),
                         proactive.KIND_CALLBACK)
        p.record_spoken(proactive.KIND_CALLBACK, self.now)
        later = self.now + timedelta(minutes=30)
        self.assertIsNone(p.decide(later, event="panel_open", callback="在准备考研"))

    def test_callback_yields_to_sit(self):
        p = self.make()
        kind = p.decide(self.now, event="panel_open", sit_minutes=120,
                        callback="在准备考研")
        self.assertEqual(kind, proactive.KIND_SIT)

    def test_callback_not_in_quiet_hours(self):
        # 夜里可以关心一句"太晚了"，但不该翻旧账
        late = datetime(2026, 11, 20, 23, 40, tzinfo=proactive.LOCAL_TZ)
        p = self.make()
        kind = p.decide(late, event="panel_open", callback="在准备考研")
        self.assertNotEqual(kind, proactive.KIND_CALLBACK)

    def test_notice_cue_mentions_the_fact_not_a_list(self):
        cue = proactive.CUES[proactive.KIND_NOTICE]
        self.assertIn("一句话", cue)
        self.assertIn("还有多久", cue)

    def test_callback_cue_forbids_database_talk(self):
        cue = proactive.CUES[proactive.KIND_CALLBACK]
        self.assertIn("根据我的记忆", cue)   # 明确列为禁止
        self.assertIn("不是人", cue)


if __name__ == "__main__":
    unittest.main()
