import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import core


class FakeClient:
    """记录每一次调用，按顺序吐预设的回答。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        reply = self.replies.pop(0) if self.replies else ""
        return {"choices": [{"message": {"content": reply}}]}

    def stream(self, *a, **kw):          # 用不到，占位
        raise AssertionError("deep_answer 不该走流式")


class DeepAnswerTests(unittest.TestCase):
    def setUp(self):
        self.original = core.CLIENT

    def tearDown(self):
        core.CLIENT = self.original

    def test_three_passes_when_critic_finds_problems(self):
        fake = FakeClient([
            "初版：明天会下雨，记得带伞。",              # 草稿
            "1. 没有依据：你没查天气就说会下雨。",        # 批评
            "改后：我没查过天气，让我先看一下。",          # 重写
        ])
        core.CLIENT = fake
        answer = core.deep_answer("明天要带伞吗")
        self.assertEqual(answer, "改后：我没查过天气，让我先看一下。")
        self.assertEqual(len(fake.calls), 3)

    def test_critique_is_fed_into_the_rewrite(self):
        fake = FakeClient([
            "初版回答",
            "1. 编了一个数字",
            "重写后的回答",
        ])
        core.CLIENT = fake
        core.deep_answer("随便问点什么")
        rewrite_system = fake.calls[2]["messages"][0]["content"]
        self.assertIn("编了一个数字", rewrite_system, "批评必须真的进了重写那一轮")
        self.assertIn("重写", rewrite_system)

    def test_skips_rewrite_when_critic_is_happy(self):
        fake = FakeClient(["很稳的回答", "没有大问题"])
        core.CLIENT = fake
        answer = core.deep_answer("1+1")
        self.assertEqual(answer, "很稳的回答")
        self.assertEqual(len(fake.calls), 2, "批评说没问题就不该再问一遍")

    def test_empty_draft_returns_empty_without_critique(self):
        fake = FakeClient([""])
        core.CLIENT = fake
        self.assertEqual(core.deep_answer("问"), "")
        self.assertEqual(len(fake.calls), 1)

    def test_empty_critique_keeps_draft(self):
        fake = FakeClient(["草稿", ""])
        core.CLIENT = fake
        self.assertEqual(core.deep_answer("问"), "草稿")

    def test_empty_rewrite_falls_back_to_draft(self):
        fake = FakeClient(["草稿", "有问题", ""])
        core.CLIENT = fake
        self.assertEqual(core.deep_answer("问"), "草稿")

    def test_question_reaches_both_passes(self):
        fake = FakeClient(["草稿", "没有大问题"])
        core.CLIENT = fake
        core.deep_answer("明年该不该换工作")
        self.assertEqual(fake.calls[0]["messages"][1]["content"], "明年该不该换工作")
        self.assertIn("明年该不该换工作", fake.calls[1]["messages"][1]["content"])

    def test_uses_low_temperature_for_critique(self):
        fake = FakeClient(["草稿", "批评", "重写"])
        core.CLIENT = fake
        core.deep_answer("问")
        self.assertLessEqual(fake.calls[1]["temperature"], 0.3)

    def test_critic_prompt_forbids_rewriting(self):
        fake = FakeClient(["草稿", "批评", "重写"])
        core.CLIENT = fake
        core.deep_answer("问")
        critic_system = fake.calls[1]["messages"][0]["content"]
        self.assertIn("挑毛病", critic_system)
        self.assertIn("不要重写", critic_system)


if __name__ == "__main__":
    unittest.main()
