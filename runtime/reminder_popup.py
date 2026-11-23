"""提醒弹窗进程。reminder.py 只管数据，这里只管把到点的提醒说出口。"""

import json
import sys
import tkinter as tk
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from reminder import ReminderStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STORE = ReminderStore(ROOT / "memory" / "reminders.json")
POLL_MS = 20000


def load_name():
    """名字在 config.json 里，跟桌宠、编排层是同一个来源。"""
    try:
        data = json.loads((ROOT / "runtime" / "config.json").read_text(encoding="utf-8") or "{}")
        return str(data.get("name") or "0").strip()[:24] or "0"
    except (OSError, json.JSONDecodeError):
        return "0"


NAME = load_name()


def show(text, on_close):
    popup = tk.Tk()
    popup.title(NAME)
    popup.attributes("-topmost", True)
    popup.configure(bg="#ffffff")
    popup.geometry("+%d+%d" % (popup.winfo_screenwidth() - 420, popup.winfo_screenheight() - 260))
    tk.Label(popup, text=f"{NAME} 提醒你", font=("Microsoft YaHei UI", 10), fg="#8a8a8a",
             bg="#ffffff").pack(anchor="w", padx=20, pady=(16, 0))
    tk.Label(popup, text=text, font=("Microsoft YaHei UI", 13), bg="#ffffff",
             wraplength=340, justify="left").pack(anchor="w", padx=20, pady=(6, 16))
    tk.Button(popup, text="知道了", command=lambda: (popup.destroy(), on_close()),
              width=12).pack(anchor="e", padx=20, pady=(0, 14))
    popup.after(180000, lambda: (popup.destroy(), on_close()))
    popup.mainloop()


def tick(root):
    try:
        for item in STORE.due():
            STORE.mark_fired(item["id"])
            show(item["text"], on_close=lambda: root.after(POLL_MS, lambda: tick(root)))
            return  # 一次一个，别糊一脸
    except Exception as exc:  # 提醒进程不能因为一次异常就死掉
        print(f"reminder tick failed: {exc}", file=sys.stderr)
    root.after(POLL_MS, lambda: tick(root))


def main():
    root = tk.Tk()
    root.withdraw()
    root.after(1500, lambda: tick(root))
    root.mainloop()


if __name__ == "__main__":
    main()
