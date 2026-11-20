"""提交前最后一道闸：确认仓库里没有私人东西。

在你**准备推送的那个目录**里跑：

    python scripts\\verify_clean.py
    python scripts\\verify_clean.py --strict      # 连图片、二进制也不放过

为什么要有这个：`git add -A` 会把当时目录里的一切都加进去，包括你以为
已经忽略掉的 `memory/`。历史上传过的东西删不干净，所以检查必须发生在
commit 之前，而不是 push 之后才发现。

分两档：
  · **硬失败**：泄露身份或许可证风险的（作者用户名路径、真实密钥、受版权保护的名字）。
  · **提醒**：任何盘符路径。示例里的 `D:\\某个文件夹` 不算问题，但值得看一眼。
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 硬失败：这些东西一出现就必须停下来
HARD = [
    (r"[A-Za-z]:\\{1,2}Users\\{1,2}[^\\\s]+", "作者的用户目录路径"),
    (r"(?i)\b(?:sk|ghp|gho|xox[baprs])[-_][A-Za-z0-9]{16,}", "疑似真实密钥"),
    (r"(?i)钢铁侠|星期五|JARVIS|斯塔克|iron\s?man|stark\s?industries", "受版权保护的虚构角色或公司名"),
]

# 提醒：不一定是问题，但提交前应该扫一眼
SOFT = [
    (r"(?<![A-Za-z])[A-Za-z]:[\\/](?![\\/])", "盘符路径（示例里可以接受）"),
]

# 绝对不该进仓库的文件 / 目录
FORBIDDEN_PATHS = ["memory", "logs", "models"]
FORBIDDEN_NAMES = {".zero_token", "config.json", "pids.json", "personality.md"}

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".gguf", ".pyc", ".zip"}


def scan(pattern, text):
    return [m.group(0) for m in re.finditer(pattern, text)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="提醒也当成失败")
    args = parser.parse_args()

    hard, soft, checked = [], [], 0

    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        parts = [p.lower() for p in rel.parts]

        if ".git" in parts:
            continue
        checked += 1

        if set(parts[:-1]) & set(FORBIDDEN_PATHS):
            hard.append(f"{rel}：这个目录不该进仓库")
            continue
        if rel.name in FORBIDDEN_NAMES:
            hard.append(f"{rel}：这个文件属于你个人，不该上传")
            continue

        if path.suffix.lower() in BINARY_SUFFIXES:
            if args.strict:
                soft.append(f"{rel}：二进制/图片，确认过没有敏感内容再提交")
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        # 验证器自己必须写出这些词才能检测，跳过它自己
        if rel.as_posix() != "scripts/verify_clean.py":
            for pattern, reason in HARD:
                found = scan(pattern, text)
                if found:
                    hard.append(f"{rel}：{reason}：{found[:3]}")

        for pattern, reason in SOFT:
            found = scan(pattern, text)
            if found:
                soft.append(f"{rel}：{reason}：{found[:3]}")

    print(f"检查了 {checked} 个文件")

    if soft:
        print(f"\n提醒 {len(soft)} 处（多半没事，看一眼）：")
        for item in soft:
            print("  ·", item)

    if hard:
        print(f"\n必须处理 {len(hard)} 处：")
        for item in hard:
            print("  -", item)
        return 1

    if args.strict and soft:
        print("\n--strict：上面的提醒也算失败。")
        return 1

    print("\n可以提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
