import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import setup


class SetupTests(unittest.TestCase):
    """第一次使用：四个问题，生成一个能开口说话的起点。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = self.root / "runtime" / "config.json"
        self.persona = self.root / "runtime" / "personality.md"
        self.patches = [
            mock.patch.object(setup, "RUNTIME", self.root / "runtime"),
            mock.patch.object(setup, "CONFIG", self.config),
            mock.patch.object(setup, "PERSONA", self.persona),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in self.patches:
            patch.stop()
        self.tmp.cleanup()

    def answers(self, **over):
        base = {"name": "小七", "city": "上海", "style": "简短", "relation": "搭档"}
        base.update(over)
        return base

    def test_writes_config_and_persona(self):
        setup.apply(self.answers(), quiet=True)
        self.assertTrue(self.config.exists())
        self.assertTrue(self.persona.exists())

    def test_name_reaches_config(self):
        setup.apply(self.answers(name="阿青"), quiet=True)
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "阿青")

    def test_name_reaches_persona(self):
        setup.apply(self.answers(name="阿青"), quiet=True)
        self.assertIn("你叫「阿青」", self.persona.read_text(encoding="utf-8"))

    def test_persona_has_the_required_sections(self):
        setup.apply(self.answers(), quiet=True)
        text = self.persona.read_text(encoding="utf-8")
        for section in ("你是谁", "怎么说话", "什么时候闭嘴", "你没有身体", "底线"):
            self.assertIn(f"## {section}", text)

    def test_persona_forbids_service_speak(self):
        setup.apply(self.answers(), quiet=True)
        self.assertIn("感谢您的提问", self.persona.read_text(encoding="utf-8"))

    def test_persona_does_not_write_sentences_she_should_not_say(self):
        # 反例是会被她复述的，只能写正确的话
        setup.apply(self.answers(), quiet=True)
        text = self.persona.read_text(encoding="utf-8")
        self.assertNotIn("不要说", text)

    def test_style_changes_the_wording(self):
        setup.apply(self.answers(style="严肃"), quiet=True)
        text = self.persona.read_text(encoding="utf-8")
        self.assertIn("先结论后理由", text)

    def test_relation_changes_the_wording(self):
        setup.apply(self.answers(relation="长辈"), quiet=True)
        self.assertIn("长辈", self.persona.read_text(encoding="utf-8"))

    def test_does_not_overwrite_an_existing_persona(self):
        self.persona.parent.mkdir(parents=True, exist_ok=True)
        self.persona.write_text("# 我自己写的\n", encoding="utf-8")
        setup.apply(self.answers(name="别人"), quiet=True)
        self.assertEqual(self.persona.read_text(encoding="utf-8"), "# 我自己写的\n")
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "别人")

    def test_keeps_existing_config_values(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"model_url": "http://x:9", "port": 1234}), encoding="utf-8")
        setup.apply(self.answers(), quiet=True)
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["model_url"], "http://x:9")
        self.assertEqual(data["port"], 1234)

    def test_defaults_fill_in_when_empty(self):
        answers = self.answers(name="", city="")
        setup.apply(answers, quiet=True)
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertTrue(data["name"])
        self.assertTrue(data["city"])

    def test_non_interactive_flags(self):
        code = setup.main(["--name", "小零", "--city", "北京",
                           "--style", "温和", "--relation", "秘书"])
        self.assertEqual(code, 0)
        data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "小零")
        self.assertEqual(data["city"], "北京")
        # 写进人格的是那句话本身，不是"温和"这个标签
        self.assertIn(setup.STYLE_NOTES["温和"], self.persona.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
