import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import core
import persona


class SmokeTests(unittest.TestCase):
    def test_prompt_builds_without_a_persona_file(self):
        # 没写 personality.md 的时候也得能起，用内置的兜底
        prompt = core.build_system_prompt(persona.MODE_CHAT)
        self.assertTrue(prompt.strip())
        self.assertIn("## 你记得的", prompt)

    def test_every_mode_has_a_block(self):
        for mode in (persona.MODE_CHAT, persona.MODE_COMFORT,
                     persona.MODE_WORK, persona.MODE_URGENT):
            self.assertIn("现在的状态", core.build_system_prompt(mode))

    def test_needs_tools_is_conservative(self):
        self.assertFalse(core.needs_tools("今天有点累"))
        self.assertTrue(core.needs_tools("今天要带伞吗"))


if __name__ == "__main__":
    unittest.main()
