import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import persona


class ClassifyTests(unittest.TestCase):
    def test_tired_is_comfort_not_task(self):
        self.assertEqual(persona.classify("今天有点累，不太想动"), persona.MODE_COMFORT)

    def test_comfort_beats_work(self):
        # 「帮我」是任务词，但人在诉苦，情绪优先
        self.assertEqual(persona.classify("我今天好烦，帮我出出主意"), persona.MODE_COMFORT)

    def test_urgent_beats_everything(self):
        self.assertEqual(persona.classify("赶紧的，我快来不及了"), persona.MODE_URGENT)

    def test_plain_task_is_work(self):
        self.assertEqual(persona.classify("帮我整理一下这周的会议记录"), persona.MODE_WORK)

    def test_smalltalk_is_chat(self):
        self.assertEqual(persona.classify("在吗"), persona.MODE_CHAT)

    def test_sticky_comfort_on_very_short_followup(self):
        # 主人诉苦之后只说了「嗯」，不该跳回闲聊模式去开玩笑
        self.assertEqual(persona.classify("嗯", persona.MODE_COMFORT), persona.MODE_COMFORT)

    def test_sticky_does_not_swallow_a_request(self):
        # 「给我讲个笑话吧」是请求，不是情绪延续，不能被粘在陪伴模式里
        self.assertEqual(persona.classify("给我讲个笑话吧", persona.MODE_COMFORT), persona.MODE_CHAT)

    def test_sticky_does_not_trap_work(self):
        self.assertEqual(persona.classify("帮我把文件重命名", persona.MODE_COMFORT), persona.MODE_WORK)

    def test_empty_is_chat(self):
        self.assertEqual(persona.classify(""), persona.MODE_CHAT)


class QuipTests(unittest.TestCase):
    def test_only_chat_mode_allows_quips(self):
        self.assertTrue(persona.allows_quip(persona.MODE_CHAT))
        for mode in (persona.MODE_COMFORT, persona.MODE_WORK, persona.MODE_URGENT):
            self.assertFalse(persona.allows_quip(mode), mode)

    def test_comfort_forbids_jokes_without_announcing_it(self):
        # 光说"不许开玩笑"不够：上一版模型会照着念"今天不讲笑话"，那也是一种吵。
        block = persona.mode_block(persona.MODE_COMFORT)
        self.assertIn("不许开玩笑", block)
        self.assertIn("不要说「我不开玩笑」", block)

    def test_chat_does_not_tell_model_to_count_jokes(self):
        # "今天最多一次俏皮话"会被模型复述成"今天不讲了"，改成让它自然反应
        block = persona.mode_block(persona.MODE_CHAT)
        self.assertNotIn("最多一次", block)
        self.assertIn("别硬凑", block)

    def test_work_block_asks_for_the_actual_deliverable(self):
        self.assertIn("直接把成品给他", persona.mode_block(persona.MODE_WORK))

    def test_citation_free_modes_are_chat_and_comfort(self):
        self.assertIn(persona.MODE_CHAT, persona.CITATION_FREE_MODES)
        self.assertIn(persona.MODE_COMFORT, persona.CITATION_FREE_MODES)


class ProfileTests(unittest.TestCase):
    def test_work_is_colder_and_longer_than_chat(self):
        chat, work = persona.profile_for(persona.MODE_CHAT), persona.profile_for(persona.MODE_WORK)
        self.assertLess(work["temperature"], chat["temperature"])
        self.assertGreater(work["max_tokens"], chat["max_tokens"])

    def test_every_mode_has_a_profile(self):
        for mode in (persona.MODE_CHAT, persona.MODE_COMFORT, persona.MODE_WORK, persona.MODE_URGENT):
            self.assertIn("max_tokens", persona.profile_for(mode))

    def test_max_tokens_is_no_longer_starving(self):
        # 上一版的 160 会把推理模型的思考链截断成空回答
        for mode in (persona.MODE_CHAT, persona.MODE_COMFORT, persona.MODE_WORK, persona.MODE_URGENT):
            self.assertGreaterEqual(persona.profile_for(mode)["max_tokens"], 300, mode)


if __name__ == "__main__":
    unittest.main()
