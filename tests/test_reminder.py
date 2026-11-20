import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
from reminder import LOCAL_TZ, ReminderStore, parse_when


class ParseWhenTests(unittest.TestCase):
    def test_relative_minutes(self):
        got = parse_when("+30m")
        delta = (got - datetime.now(LOCAL_TZ)).total_seconds()
        self.assertAlmostEqual(delta, 1800, delta=30)

    def test_relative_hours(self):
        got = parse_when("+2h")
        self.assertAlmostEqual((got - datetime.now(LOCAL_TZ)).total_seconds(), 7200, delta=30)

    def test_clock_time_rolls_to_tomorrow(self):
        got = parse_when("00:01")
        self.assertGreater(got, datetime.now(LOCAL_TZ))
        self.assertLess(got, datetime.now(LOCAL_TZ) + timedelta(days=2))

    def test_iso_datetime(self):
        self.assertEqual(parse_when("2027-03-04T09:30").hour, 9)

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_when("下周三下午")
        with self.assertRaises(ValueError):
            parse_when("")


class ReminderStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "reminders.json"
        self.store = ReminderStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def _write_past(self, minutes_ago=5, repeat="none"):
        moment = datetime.now(LOCAL_TZ) - timedelta(minutes=minutes_ago)
        self.path.write_text(json.dumps([{
            "id": "rm_x", "text": "旧提醒", "at": moment.isoformat(timespec="minutes"),
            "next_fire_at": moment.isoformat(timespec="seconds"), "repeat": repeat,
            "enabled": True, "fired_count": 0, "last_fired_at": None,
        }], ensure_ascii=False), encoding="utf-8")

    def test_add_and_list(self):
        item = self.store.add("喝水", "+10m")
        self.assertEqual(len(self.store.all()), 1)
        self.assertEqual(item["repeat"], "none")

    def test_missed_reminder_still_fires(self):
        # 上一版的 bug：睡过那一分钟就永久丢失。现在过点了也要发。
        self._write_past(minutes_ago=30)
        due = self.store.due()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["text"], "旧提醒")

    def test_future_reminder_not_due(self):
        self.store.add("明天的事", "+1d")
        self.assertEqual(self.store.due(), [])

    def test_one_shot_disables_after_firing(self):
        self._write_past()
        self.store.mark_fired("rm_x")
        item = self.store.all()[0]
        self.assertFalse(item["enabled"])
        self.assertEqual(self.store.due(), [])

    def test_daily_reschedules_to_next_day(self):
        self._write_past(minutes_ago=5, repeat="daily")
        self.store.mark_fired("rm_x")
        item = self.store.all()[0]
        self.assertTrue(item["enabled"])
        following = datetime.fromisoformat(item["next_fire_at"])
        self.assertGreater(following, datetime.now(LOCAL_TZ))
        self.assertEqual(self.store.due(), [])

    def test_weekdays_skips_weekend(self):
        moment = datetime(2026, 11, 13, 9, 0, tzinfo=LOCAL_TZ)  # 周五
        self.path.write_text(json.dumps([{
            "id": "rm_w", "text": "上班", "at": moment.isoformat(timespec="minutes"),
            "next_fire_at": moment.isoformat(timespec="seconds"), "repeat": "weekdays",
            "enabled": True, "fired_count": 0, "last_fired_at": None,
        }], ensure_ascii=False), encoding="utf-8")
        self.store.mark_fired("rm_w", moment=moment)
        following = datetime.fromisoformat(self.store.all()[0]["next_fire_at"])
        self.assertLess(following.weekday(), 5, "工作日提醒不该排到周末")

    def test_fired_count_increments(self):
        self._write_past()
        self.store.mark_fired("rm_x")
        self.assertEqual(self.store.all()[0]["fired_count"], 1)

    def test_remove(self):
        item = self.store.add("删掉我", "+1h")
        self.assertTrue(self.store.remove(item["id"]))
        self.assertEqual(self.store.all(), [])

    def test_remove_unknown_returns_false(self):
        self.assertFalse(self.store.remove("rm_nope"))


if __name__ == "__main__":
    unittest.main()
