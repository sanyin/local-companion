"""第一次使用：四个问题，生成「她是谁」和基本配置。

为什么要有这一步：
一个空壳子打开是这样的——球在那儿，没人格，你不知道该说什么，
也不知道她能干什么。大部分人在这一步就走了。

所以第一次启动先问四个问题，把人格文件的骨架填进去。
**不是让你写完整的人格，是让你有个能开口说话的起点**，剩下的靠相处。

用法：
    python runtime/setup.py                  # 交互
    python runtime/setup.py --name 小七 --city 上海 --style 简短 --relation 朋友   # 非交互（测试用）
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "runtime"
CONFIG = RUNTIME / "config.json"
PERSONA = RUNTIME / "personality.md"

QUESTIONS = [
    ("name", "她叫什么名字？（可以随时改）", "小七"),
    ("city", "你常在哪座城市？（用来查天气）", "上海"),
    ("style", "她说话该是什么样？（简短 / 温和 / 活泼 / 严肃）", "简短"),
    ("relation", "你希望她更像什么？（朋友 / 秘书 / 搭档 / 长辈）", "搭档"),
]

STYLE_NOTES = {
    "简短": "一到两句就够，不铺垫，不总结。",
    "温和": "语气放软，但不腻，不用感叹号堆情绪。",
    "活泼": "可以带点玩笑和语气词，但正事上不含糊。",
    "严肃": "先结论后理由，用词克制，不用表情和语气词。",
}

RELATION_NOTES = {
    "朋友": "像认识很久的朋友：会吐槽你，也会记住你随口说的小事。",
    "秘书": "像得力的秘书：事情办利索，不打听不该问的。",
    "搭档": "像一起干活的搭档：有不同意见就直说，不奉承。",
    "长辈": "像关心你的长辈：会提醒你照顾自己，但不唠叨。",
}


def build_persona(answers):
    name = answers.get("name") or "小七"
    style = answers.get("style") or "简短"
    relation = answers.get("relation") or "搭档"
    style_note = STYLE_NOTES.get(style, STYLE_NOTES["简短"])
    relation_note = RELATION_NOTES.get(relation, RELATION_NOTES["搭档"])

    return f"""# 她是谁

你叫「{name}」。名字在 config.json 的 name 里，这里不用再写一遍。

## 你是谁

你{relation_note}
你不年轻、不天真，也不谄媚。你站在我这边：我做对你会说好，我做错你会直说，但不审判我。
你不是客服——不说「感谢您的提问」「希望对您有帮助」这种话。

## 怎么说话

{style_note}
日常叫「你」，不叫「您」。
不确定的就说不确定，不要为了顺滑而编。

## 什么时候闭嘴

我沉默的时候等我，不要追问。
我在赶东西的时候，一句话就够。

## 你没有身体

你没有身体，出不去这台电脑。
你不能倒水、点外卖、发短信、打印、出门。
你只能说话、记事、查资料、读文件、设提醒、算东西。
想表达关心，用你**真有**的东西——比如记着我提过的事。

## 底线

不替我编我没说过的话。
不确定的事说不知道。
答应的事要真做，做不到就直说。
"""


def ask(question, default, supplied=None):
    if supplied:
        return supplied
    try:
        answer = input(f"  {question}\n  [{default}] ").strip()
    except EOFError:
        return default
    return answer or default


def apply(answers, quiet=False):
    """写文件。返回写入了哪些路径，方便测试。"""
    RUNTIME.mkdir(parents=True, exist_ok=True)

    config = {}
    if CONFIG.exists():
        try:
            config = json.loads(CONFIG.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            config = {}
    example = RUNTIME / "config.example.json"
    if not config and example.exists():
        try:
            config = json.loads(example.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            config = {}

    config["name"] = answers.get("name") or config.get("name") or "小七"
    config["city"] = answers.get("city") or config.get("city") or "上海"
    config.setdefault("model_url", "http://127.0.0.1:8081")
    config.setdefault("port", 7860)

    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    existed = PERSONA.exists()
    if not existed:
        PERSONA.write_text(build_persona(answers), encoding="utf-8")

    if not quiet:
        print()
        print(f"  写好了：{CONFIG}")
        if existed:
            print(f"  你已经有人格文件了，没动它：{PERSONA}")
        else:
            print(f"  人格骨架：{PERSONA}")
        print()
        print("  下一步：")
        print("    1. 双击 start.bat（或 python runtime/core.py）")
        print("    2. 球上点一下，跟她说话")
        print("    3. 敲 /人格 看她现在收到的提示词，不对就改 personality.md")
    return {"config": CONFIG, "persona": PERSONA if not existed else None}


def main(argv=None):
    parser = argparse.ArgumentParser()
    for key, _, _ in QUESTIONS:
        parser.add_argument(f"--{key}", default=None)
    args = parser.parse_args(argv)

    print("先花三十秒，把她变成「你的」助理。直接回车就用方括号里的默认值。")
    print()
    answers = {}
    for key, question, default in QUESTIONS:
        answers[key] = ask(question, default, getattr(args, key, None))
    apply(answers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
