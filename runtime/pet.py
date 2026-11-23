"""本地助理的桌面形态。

这一版是把桌宠当成**唯一界面**来做的，因为它就是主人日常用的那个东西。

修掉的问题：
  · 上一版用 pack 布局，聊天区的 Text 默认高 24 行，把整个窗口吃光，
    输入框只分到 1x1、根本没被映射出来——点开面板看不到任何能点的地方。
    现在用 grid，行/列的伸展权重写死，输入行永远在底部有位置。
  · 面板不贴着球开，跑到屏幕左上角。现在贴着球开。
  · 没有地方看记忆。现在有 /记忆 /收 /忘 这几个迷你命令。

网络一律在后台线程，界面只碰主线程的队列（tkinter 不是线程安全的）。
"""

import ctypes
import json
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CORE = "http://127.0.0.1:7860"
TOKEN_FILE = ROOT / "runtime" / ".zero_token"
CONFIG_FILE = ROOT / "runtime" / "config.json"
SESSION = "pet"

DEFAULT_NAME = "0"


def load_name():
    """名字在 config.json 里。桌宠是独立进程，所以自己去读，不依赖 core。"""
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8") or "{}")
        return str(data.get("name") or DEFAULT_NAME).strip()[:24] or DEFAULT_NAME
    except (OSError, json.JSONDecodeError):
        return DEFAULT_NAME


def ball_text(name):
    """球只有那么大。两个字以内直接显示，再长就取首字。"""
    name = (name or DEFAULT_NAME).strip()
    return name if len(name) <= 2 else name[0]

STATE_COLORS = {
    "idle": ("#f7f4ee", "#e2dcd0"),
    "thinking": ("#dbe8ff", "#b8cdf0"),
    "tool": ("#d9f5ee", "#a8ddd0"),
    "done": ("#fff0c2", "#e8d18a"),
    "confirm": ("#ffe0bb", "#e8b98a"),
    "error": ("#ffdcdc", "#e8a8a8"),
}
STATE_LABEL = {
    "idle": "", "thinking": "想着…", "tool": "在动手",
    "done": "", "confirm": "等你点头", "error": "连不上",
}

BG = "#14151a"
PANEL = "#1b1d24"
INPUT = "#23262f"
TEXT = "#e8e9ee"
DIM = "#8b90a0"
ACCENT = "#7fb4ff"

HELP = (
    "直接说话就行。\n"
    "顶上的「记忆」能点开，看她在记什么、收下还是不要——不用敲命令。\n"
    "「简报」是今天的天气和要闻。\n\n"
    "习惯打字的话，这些也可以：\n"
    "  /简报        今天的天气和要闻\n"
    "  /记忆        打开记忆窗口\n"
    "  /记 内容     直接让她记住一句话\n"
    "  /授权 路径   允许她读某个文件夹（比如 /授权 <你的文件夹>）\n"
    "  /目录        看现在授权了哪些\n"
    "  /试 [早|夜|坐]  让她现在就主动说一句\n"
    "  /深思 问题   让她多想一遍（慢，但更靠谱）\n"
    "  /名字 新名字  给她改个名字（名字是使用者起的，随时能换）\n"
    "  /人格        看她的系统提示现在长什么样（调人格时用）\n"
    "  /新          开个新话题（只断上下文，不清屏幕）\n"
    "  /帮助        这条说明"
)


def read_token():
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def system_idle_seconds():
    """系统多久没人动键鼠了。判断"他还在不在电脑前"用这个，比自己记时准。"""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        elapsed = int(ctypes.windll.kernel32.GetTickCount()) - int(info.dwTime)
        return max(0.0, elapsed / 1000.0)
    except Exception:
        return 0.0


class Pet:
    def __init__(self):
        self.events = queue.Queue()
        self.state = "idle"
        self.token = read_token()
        self.busy = False
        self.session = SESSION
        self.panel = None
        self.transcript = None
        self.entry = None
        self.status = None
        self.memory_ids = []
        self.mem_window = None
        self.mem_inner = None
        self.mem_summary = None
        self.memory_button = None
        self._drag_start = None
        self._moved = False
        # 主动开口用到的几个量
        self.bubble = None
        self.sit_seconds = 0.0
        self.last_tick = time.time()
        self.last_user_at = 0.0
        self.last_proactive_at = 0.0
        self._hinted_pending = False

        self.name = load_name()
        self.prompt_dir = ROOT / "logs"

        self.root = tk.Tk()
        self.root.title(self.name)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.canvas = tk.Canvas(self.root, width=84, height=84, bg=BG, highlightthickness=0)
        self.canvas.pack()
        self.ball = self.canvas.create_oval(10, 10, 74, 74, fill=STATE_COLORS["idle"][0],
                                            outline=STATE_COLORS["idle"][1], width=1)
        self.ball_label = self.canvas.create_text(
            42, 43, text=ball_text(self.name), fill="#9aa0ad", font=("Segoe UI", 22, "bold"))

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.canvas.bind("<Button-3>", self.popup_menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="说句话", command=self.open_panel)
        self.menu.add_command(label="记忆", command=self.open_memory_window)
        self.menu.add_command(label="今天的简报", command=lambda: (self.open_panel(), self.show_brief()))
        self.menu.add_command(label="网页版", command=lambda: webbrowser.open(CORE))
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.quit)

        self.root.update_idletasks()
        self.ball_x = self.root.winfo_screenwidth() - 140
        self.ball_y = self.root.winfo_screenheight() - 220
        self.root.geometry(f"84x84+{self.ball_x}+{self.ball_y}")
        self.root.deiconify()
        self.root.lift()
        self.root.after(100, lambda: self.root.attributes("-topmost", True))
        self.root.after(60, self.drain)
        self.root.after(200, lambda: threading.Thread(target=self.watchdog, daemon=True).start())
        self.root.after(3000, lambda: threading.Thread(target=self.proactive_loop, daemon=True).start())
        self.root.after(5000, lambda: threading.Thread(target=self.tasks_loop, daemon=True).start())

    # ---------- 拖动 / 点击 ----------

    def on_press(self, event):
        self._drag_start = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
        self._moved = False

    def on_drag(self, event):
        if not self._drag_start:
            return
        sx, sy, wx, wy = self._drag_start
        dx, dy = event.x_root - sx, event.y_root - sy
        if abs(dx) + abs(dy) > 3:
            self._moved = True
        self.root.geometry(f"+{wx + dx}+{wy + dy}")
        self.ball_x, self.ball_y = wx + dx, wy + dy

    def on_release(self, event):
        self._drag_start = None
        if not self._moved:
            self.open_panel()

    def popup_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ---------- 状态 ----------

    def set_state(self, state):
        if state == self.state:
            return
        self.state = state
        fill, outline = STATE_COLORS.get(state, STATE_COLORS["idle"])
        self.canvas.itemconfig(self.ball, fill=fill, outline=outline, width=2 if state != "idle" else 1)
        if self.status is not None:
            self.status.config(text=STATE_LABEL.get(state, ""))

    def drain(self):
        """主线程唯一碰控件的地方。"""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "state":
                    self.set_state(payload)
                elif kind == "delta":
                    self.append(payload, "zero", newline=False)
                elif kind == "line":
                    self.append(payload, "zero")
                elif kind == "note":
                    self.append(payload, "note")
                elif kind == "user":
                    self.append("你：" + payload, "me")
                elif kind == "confirm":
                    self.show_confirm(payload)
                elif kind == "speak":
                    self.present_speech(payload)
                elif kind == "memories":
                    self.render_memories(payload)
                elif kind == "task_done":
                    self.present_task(payload)
                elif kind == "renamed":
                    self.apply_rename(payload)
                elif kind == "refresh_memory":
                    self._refresh_memory_window()
                elif kind == "done":
                    self.finish_turn(payload)
                elif kind == "clear":
                    self.clear_transcript()
        except queue.Empty:
            pass
        finally:
            self.root.after(60, self.drain)

    # ---------- 与后端 ----------

    def headers(self):
        return {"Content-Type": "application/json", "X-Zero-Token": self.token}

    def watchdog(self):
        while True:
            try:
                with urlopen(f"{CORE}/api/health", timeout=3) as response:
                    data = json.loads(response.read() or b"{}")
                ok = (data.get("model") or {}).get("status") == "ok"
                if not self.busy:
                    self.events.put(("state", "idle" if ok else "error"))
            except Exception:
                if not self.busy:
                    self.events.put(("state", "error"))
            time.sleep(4)

    # ---------- 后台任务 ----------

    def tasks_loop(self):
        """盯着后台队列。活干完了就说一声——这是"分身"能被感知到的唯一方式。"""
        seen = None
        while True:
            time.sleep(10)
            try:
                with urlopen(Request(f"{CORE}/api/tasks", headers=self.headers()), timeout=15) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception:
                continue
            tasks = data.get("tasks") or []
            if seen is None:
                # 第一次只记账，不把启动前就做完的活重新播一遍
                seen = {t["id"]: t["status"] for t in tasks}
                continue
            for task in tasks:
                previous = seen.get(task["id"])
                if task["status"] in ("done", "failed") and previous not in ("done", "failed"):
                    self.events.put(("task_done", task))
                seen[task["id"]] = task["status"]

    def present_task(self, task):
        title = task.get("title") or "后台的活"
        if task["status"] == "done":
            body = (task.get("result") or "").strip()
            text = f"（后台做完了）{title}\n{body}"
        else:
            text = f"（后台那个「{title}」没做成）{task.get('error','')}"
        if self.panel is not None and self.panel.winfo_exists() and self.panel.winfo_viewable():
            for line in text.splitlines():
                self.append(line, "zero" if line.strip() else "note")
        else:
            self.show_bubble(text.splitlines()[0][:120])

    # ---------- 主动开口 ----------

    def proactive_loop(self):
        """每分钟问一次后端：该不该开口。

        '要不要说' 全在后端判，这里只负责计时和把话说出来。
        故意放到 3 秒后再启动，免得开机那一下抢资源。
        """
        while True:
            time.sleep(60)
            try:
                self.accumulate_sit()
                self.check_proactive("tick")
            except Exception:
                pass

    def accumulate_sit(self):
        now = time.time()
        elapsed = now - self.last_tick
        self.last_tick = now
        if elapsed <= 0 or elapsed > 180:  # 机器睡过一觉，不算连续坐着
            return
        if system_idle_seconds() < 300:
            self.sit_seconds += elapsed
        else:
            self.sit_seconds = 0.0

    def recent_user_minutes(self):
        if not self.last_user_at:
            return 999
        return (time.time() - self.last_user_at) / 60.0

    def check_proactive(self, event):
        if self.busy:
            return
        # 刚说过话就别紧接着又来一句
        if self.last_proactive_at and time.time() - self.last_proactive_at < 600:
            return
        payload = {
            "event": event,
            "sit_minutes": int(self.sit_seconds // 60),
            "recent_user_minutes": int(self.recent_user_minutes()),
        }

        def work():
            request = Request(f"{CORE}/api/proactive/check",
                              data=json.dumps(payload).encode(),
                              headers=self.headers(), method="POST")
            try:
                with urlopen(request, timeout=90) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception:
                return
            if data.get("text"):
                self.events.put(("speak", data))
                self.sit_seconds = 0.0

        threading.Thread(target=work, daemon=True).start()

    def present_speech(self, data):
        """她主动说了一句。面板开着就写进对话，没开就冒个气泡。"""
        self.last_proactive_at = time.time()
        text = data.get("text", "").strip()
        if not text:
            return
        if self.panel is not None and self.panel.winfo_exists() and self.panel.winfo_viewable():
            self.append(text, "zero")
        else:
            self.show_bubble(text)

    def show_bubble(self, text):
        self.close_bubble()
        bubble = tk.Toplevel(self.root)
        bubble.overrideredirect(True)
        bubble.attributes("-topmost", True)
        frame = tk.Frame(bubble, bg="#2a2f3a", highlightthickness=1, highlightbackground="#404859")
        frame.pack()
        label = tk.Label(frame, text=text, bg="#2a2f3a", fg=TEXT, wraplength=250,
                         justify="left", font=("Microsoft YaHei UI", 10), padx=14, pady=10)
        label.pack()
        bubble.update_idletasks()
        width = max(bubble.winfo_reqwidth(), 120)
        height = bubble.winfo_reqheight()
        x = min(max(self.ball_x - width + 84, 8), self.root.winfo_screenwidth() - width - 8)
        y = max(self.ball_y - height - 10, 8)
        bubble.geometry(f"+{x}+{y}")
        for widget in (bubble, frame, label):
            widget.bind("<Button-1>", lambda _e: (self.close_bubble(), self.open_panel()))
        bubble.after(16000, self.close_bubble)
        self.bubble = bubble

    def close_bubble(self):
        if self.bubble is not None:
            try:
                self.bubble.destroy()
            except tk.TclError:
                pass
            self.bubble = None

    def quit(self):
        self.close_bubble()
        self.root.destroy()

    # ---------- 发送 ----------

    def submit(self):
        if self.entry is None:
            return
        text = self.entry.get().strip()
        if not text:
            return
        self.entry.delete(0, "end")
        self.last_user_at = time.time()
        if text.startswith("/"):
            self.command(text)
            return
        self.send(text)

    def send(self, text):
        if self.busy or not text.strip():
            return
        self.busy = True
        self.last_user_at = time.time()
        self.events.put(("user", text))
        self.events.put(("state", "thinking"))
        if self.entry is not None:
            self.entry.config(state="disabled")
        threading.Thread(target=self.chat_worker, args=(text, self.session), daemon=True).start()

    def chat_worker(self, text, session):
        body = json.dumps({"text": text, "session": session}).encode("utf-8")
        request = Request(f"{CORE}/api/chat", data=body, headers=self.headers())
        try:
            with urlopen(request, timeout=300) as response:
                buffer = ""
                for raw in response:
                    buffer += raw.decode("utf-8", "replace")
                    while "\n\n" in buffer:
                        block, buffer = buffer.split("\n\n", 1)
                        self.handle_block(block)
        except Exception as exc:
            self.events.put(("note", f"连不上：{exc}"))
            self.events.put(("state", "error"))
        finally:
            self.events.put(("done", None))

    def handle_block(self, block):
        name, payload = "message", "{}"
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                payload = line[5:].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return
        if name == "delta":
            self.events.put(("delta", data.get("text", "")))
        elif name == "meta":
            self.events.put(("state", "thinking"))
        elif name == "tool":
            self.events.put(("state", "tool"))
            self.events.put(("note", ("⚙ " if data.get("ok") else "⚠ ") + data.get("summary", "")))
        elif name == "confirm":
            self.events.put(("state", "confirm"))
            self.events.put(("confirm", data))
        elif name == "citations":
            for item in (data.get("items") or [])[:3]:
                self.events.put(("note", "出处：" + item))
        elif name == "error":
            self.events.put(("note", "出错了：" + data.get("message", "")))
            self.events.put(("state", "error"))
        elif name == "done":
            self.events.put(("done", data))

    def finish_turn(self, payload=None):
        self.busy = False
        if self.entry is not None:
            self.entry.config(state="normal")
            self.entry.focus_set()
        self.set_state("idle")
        # 有新候选记忆时提一次，别每次都提——他嫌烦就用 /忘
        pending = (payload or {}).get("pending_memories") or 0
        if pending and not self._hinted_pending:
            self._hinted_pending = True
            self.set_memory_badge(pending)
            self.events.put(("note", f"她现在有 {pending} 条只是猜的，等你点头。"
                                    "点上面的「记忆」看一眼就行。"))

    # ---------- 迷你命令 ----------

    def command(self, text):
        parts = text.split(maxsplit=1)
        verb = parts[0][1:].lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        arg = rest.split()[0] if rest else ""
        if verb in ("帮助", "help", "?"):
            self.append(HELP, "note")
        elif verb in ("简报", "brief"):
            self.show_brief()
        elif verb in ("记忆", "mem"):
            self.open_memory_window()
        elif verb in ("收", "keep", "确认"):
            self._act_on_memory(arg, "confirm")
        elif verb in ("忘", "forget", "删"):
            self._act_on_memory(arg, "forget")
        elif verb in ("记", "记住", "remember"):
            if not rest:
                self.append("要记什么？比如 /记 我早上不喝咖啡", "note")
            else:
                self._remember(rest)
        elif verb in ("授权", "auth"):
            if not rest:
                self.append("给我一个文件夹路径，比如 /授权 <你的文件夹>", "note")
            else:
                self._authorize(rest.strip('"').strip("'"))
        elif verb in ("试", "preview", "演示"):
            self.preview_speech(arg or "return")
        elif verb in ("深思", "deep"):
            if not rest:
                self.append("想什么？比如 /深思 这份报告该怎么改", "note")
            else:
                self._deep_think(rest)
        elif verb in ("人格", "persona"):
            self._show_prompt()
        elif verb in ("名字", "name"):
            self._rename(rest)
        elif verb in ("目录", "roots"):
            self._show_roots()
        elif verb in ("新", "new"):
            self.session = f"pet_{int(time.time())}"
            # 只断上下文，不清屏幕——清掉的话刚才聊的就再也翻不回来了
            self.events.put(("note", "—————— 新话题 ——————"))
            self.events.put(("note", "上一轮的内容不带上来了，接着说吧。"))
        elif verb in ("清空", "clear"):
            self.events.put(("clear", None))
        else:
            self.append(f"没有 /{verb} 这个命令。/帮助 看看有什么。", "note")

    def preview_speech(self, kind="return"):
        """手动让她说一句，用来确认她主动开口长什么样。不占预算。"""
        aliases = {"早": "morning", "早安": "morning", "回": "return", "回来": "return",
                   "夜": "late_night", "深夜": "late_night", "坐": "sit", "久坐": "sit"}
        kind = aliases.get(kind, kind)

        def work():
            body = json.dumps({"event": "force", "kind": kind, "sit_minutes": 95}).encode()
            request = Request(f"{CORE}/api/proactive/check", data=body,
                              headers=self.headers(), method="POST")
            try:
                with urlopen(request, timeout=120) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception as exc:
                self.events.put(("note", f"没成功：{exc}"))
                return
            if data.get("text"):
                self.events.put(("speak", data))
            else:
                self.events.put(("note", "她这会儿没话说（模型可能没在跑）。"))
        threading.Thread(target=work, daemon=True).start()

    def _remember(self, text):
        """主人明确说"记下这个"——直接生效，因为这是他的命令，不是她的猜测。"""
        def work():
            body = json.dumps({"text": text, "kind": "fact"}).encode()
            request = Request(f"{CORE}/api/memories", data=body,
                              headers=self.headers(), method="POST")
            try:
                with urlopen(request, timeout=20) as response:
                    data = json.loads(response.read() or b"{}")
                item = data.get("memory")
                if item:
                    self.events.put(("note", f"记下了：{item['text']}"))
                else:
                    self.events.put(("note", "没记上。"))
            except Exception as exc:
                self.events.put(("note", f"没成功：{exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _show_prompt(self):
        """把系统提示摊开给使用者看。调人格最怕盲调——改了不知道模型到底看到了什么。"""
        def work():
            try:
                with urlopen(Request(f"{CORE}/api/prompt", headers=self.headers()), timeout=20) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception as exc:
                self.events.put(("note", f"取不到提示词：{exc}"))
                return

            preview = self.prompt_dir / "prompt-preview.md"
            preview.parent.mkdir(parents=True, exist_ok=True)
            preview.write_text(data.get("prompt") or "", encoding="utf-8")

            lines = [
                f"当前是「{data.get('mode')}」状态下的提示词：",
                f"  总长 {data.get('chars')} 字，约 {data.get('approx_tokens')} token",
                f"  其中人格 {data.get('persona_chars')} 字",
                "",
                "已经有这些小节：" + "、".join(data.get("sections_found") or []) or "（一个人格小节都没有）",
            ]
            missing = data.get("sections_missing") or []
            if missing:
                lines.append("")
                lines.append("还缺这些小节（缺了不会崩，但她会没立场）：")
                for item in missing:
                    lines.append(f"  · {item['name']}——{item['why']}")
            lines.append("")
            lines.append(f"完整内容已写到：{preview}")
            lines.append("改了 personality.md 之后不用重启，下一条消息就生效。")

            for line in lines:
                self.events.put(("line" if line.strip() else "note", line))

        threading.Thread(target=work, daemon=True).start()

    def _rename(self, value):
        """改名字。她说的是使用者给她起的名字，不是产品名。"""
        value = (value or "").strip()
        if not value:
            self.append(f"我现在叫「{self.name}」。想改就 /名字 新名字", "note")
            return
        if len(value) > 24:
            self.append("名字太长了，短一点吧。", "note")
            return

        def work():
            body = json.dumps({"name": value}).encode()
            request = Request(f"{CORE}/api/config", data=body,
                              headers=self.headers(), method="POST")
            try:
                with urlopen(request, timeout=15) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception as exc:
                self.events.put(("note", f"改名没成：{exc}"))
                return
            self.events.put(("renamed", str(data.get("name") or value)))

        threading.Thread(target=work, daemon=True).start()

    def apply_rename(self, name):
        """只碰界面。改完名字所有出现的地方都要跟着变，不然会很怪。"""
        self.name = name
        self.root.title(name)
        self.canvas.itemconfigure(self.ball_label, text=ball_text(name))
        if getattr(self, "header_label", None) is not None:
            try:
                self.header_label.configure(text=name)
            except tk.TclError:
                pass
        if self.panel is not None and self.panel.winfo_exists():
            try:
                self.panel.title(name)
            except tk.TclError:
                pass
        self.append(f"好，以后我就叫「{name}」。", "note")

    def _deep_think(self, question):
        """让她答两遍：先答，再挑自己的错，最后重写。慢，但更靠谱。"""
        self.append("（这一句我会多想一遍，慢一点）", "note")

        def work():
            body = json.dumps({"text": question}).encode()
            request = Request(f"{CORE}/api/deep", data=body, headers=self.headers(), method="POST")
            try:
                with urlopen(request, timeout=300) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception as exc:
                self.events.put(("note", f"没想成：{exc}"))
                return
            answer = (data.get("answer") or data.get("error") or "").strip()
            if not answer:
                self.events.put(("note", "她想不出该说什么。"))
                return
            for line in answer.splitlines():
                self.events.put(("line" if line.strip() else "note", line))
        threading.Thread(target=work, daemon=True).start()

    def _authorize(self, path):
        def work():
            request = Request(f"{CORE}/api/roots", data=json.dumps({"path": path}).encode(),
                              headers=self.headers(), method="POST")
            try:
                with urlopen(request, timeout=20) as response:
                    data = json.loads(response.read() or b"{}")
                roots = data.get("roots") or []
                self.events.put(("note", f"好，现在能碰 {len(roots)} 个目录了。"))
                for item in roots:
                    self.events.put(("note", "  " + item))
            except Exception as exc:
                self.events.put(("note", f"授权没成功：{exc}"))
        threading.Thread(target=work, daemon=True).start()

    def _show_roots(self):
        def work():
            try:
                with urlopen(f"{CORE}/api/health", timeout=10) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception as exc:
                self.events.put(("note", f"读不到：{exc}"))
                return
            roots = data.get("roots") or []
            if not roots:
                self.events.put(("note", "还没授权任何目录，所以她的文件工具什么都看不到。"))
                self.events.put(("note", "用 /授权 路径 给她一个，比如 /授权 <你的文件夹>"))
                return
            self.events.put(("note", "她能碰这些地方："))
            for item in roots:
                self.events.put(("note", "  " + item))
        threading.Thread(target=work, daemon=True).start()

    def show_brief(self):
        def work():
            try:
                with urlopen(Request(f"{CORE}/api/brief", headers=self.headers()), timeout=40) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception as exc:
                self.events.put(("note", f"简报没拿到：{exc}"))
                return
            brief = data.get("brief") or ""
            if brief:
                for line in brief.splitlines():
                    self.events.put(("line", line))
            for item in (data.get("reminders") or [])[:5]:
                self.events.put(("note", f"待办：{item.get('text')}（{item.get('at')}）"))
        threading.Thread(target=work, daemon=True).start()

    # ---------- 记忆窗口（点着用，不用敲命令） ----------

    def open_memory_window(self):
        if self.mem_window is not None and self.mem_window.winfo_exists():
            self.mem_window.deiconify()
            self.mem_window.lift()
            self._refresh_memory_window()
            return

        win = tk.Toplevel(self.root)
        win.title(f"{self.name} · 记忆")
        win.configure(bg=BG)
        win.minsize(380, 360)
        win.geometry("450x540")
        win.attributes("-topmost", True)
        win.grid_rowconfigure(1, weight=1)
        win.grid_columnconfigure(0, weight=1)

        head = tk.Frame(win, bg=BG)
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        tk.Label(head, text="记忆", bg=BG, fg=TEXT, font=("Segoe UI", 13, "bold")).pack(side="left")
        self.mem_summary = tk.Label(head, text="读取中…", bg=BG, fg=DIM, font=("Microsoft YaHei UI", 9))
        self.mem_summary.pack(side="left", padx=10)
        tk.Button(head, text="刷新", relief="flat", bg=BG, fg=DIM, activebackground=PANEL, bd=0,
                  font=("Microsoft YaHei UI", 9),
                  command=self._refresh_memory_window).pack(side="right")

        body = tk.Frame(win, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 14))
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(body, command=canvas.yview, width=10)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.mem_inner = tk.Frame(canvas, bg=BG)
        anchor = canvas.create_window((0, 0), window=self.mem_inner, anchor="nw")
        self.mem_inner.bind("<Configure>",
                            lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(anchor, width=e.width))
        # 滚轮：绑在窗口和画布上，别用 bind_all，会污染别的窗口
        for widget in (win, canvas):
            widget.bind("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.mem_window = win
        self._refresh_memory_window()

    def _refresh_memory_window(self):
        if self.mem_window is None or not self.mem_window.winfo_exists():
            return

        def work():
            try:
                with urlopen(Request(f"{CORE}/api/memories", headers=self.headers()), timeout=20) as response:
                    data = json.loads(response.read() or b"{}")
            except Exception as exc:
                self.events.put(("note", f"记忆没读到：{exc}"))
                return
            self.events.put(("memories", data))

        threading.Thread(target=work, daemon=True).start()

    def render_memories(self, data):
        """把记忆画成可以点的列表：待确认的每行两个按钮，已记住的每行一个。"""
        if self.mem_window is None or not self.mem_window.winfo_exists() or self.mem_inner is None:
            return
        for child in self.mem_inner.winfo_children():
            child.destroy()

        active = data.get("active") or []
        pending = data.get("pending") or []
        self.memory_ids = []
        self.set_memory_badge(len(pending))

        if self.mem_summary is not None:
            self.mem_summary.config(text=f"{len(active)} 条已记住 · {len(pending)} 条等你点头")

        inner = self.mem_inner
        width = 380

        if pending:
            tk.Label(inner, text="等你点头——这些她还没敢当真", bg=BG, fg="#ffd479",
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(6, 4))
            for item in pending:
                card = tk.Frame(inner, bg=PANEL)
                card.pack(fill="x", pady=3)
                mark = "你说过" if item.get("explicit") else "她猜的"
                tk.Label(card, text=item["text"], bg=PANEL, fg=TEXT, wraplength=width,
                         justify="left", anchor="w", font=("Microsoft YaHei UI", 10),
                         padx=10, pady=8).pack(fill="x")
                for replaced in item.get("supersedes_text") or []:
                    tk.Label(card, text=f"收下会替换：{replaced}", bg=PANEL, fg="#ff9f6e",
                             wraplength=width - 20, justify="left", anchor="w",
                             font=("Microsoft YaHei UI", 8), padx=10).pack(fill="x")
                if item.get("evidence_label"):
                    tk.Label(card, text=item["evidence_label"], bg=PANEL, fg=DIM,
                             wraplength=width - 20, justify="left", anchor="w",
                             font=("Microsoft YaHei UI", 8), padx=10).pack(fill="x")
                row = tk.Frame(card, bg=PANEL)
                row.pack(fill="x", padx=10, pady=(0, 8))
                tk.Label(row, text=mark, bg=PANEL, fg=DIM,
                         font=("Microsoft YaHei UI", 8)).pack(side="left")
                tk.Button(row, text="不要", relief="flat", bg=INPUT, fg=DIM, bd=0,
                          font=("Microsoft YaHei UI", 9),
                          command=lambda i=item["id"]: self._memory_action(i, "forget")
                          ).pack(side="right")
                tk.Button(row, text="收下", relief="flat", bg="#33507c", fg=TEXT, bd=0,
                          font=("Microsoft YaHei UI", 9),
                          command=lambda i=item["id"]: self._memory_action(i, "confirm")
                          ).pack(side="right", padx=(0, 6))

        if active:
            tk.Label(inner, text="已经记住的", bg=BG, fg=DIM,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(14, 4))
            for item in active:
                card = tk.Frame(inner, bg=INPUT)
                card.pack(fill="x", pady=3)
                tk.Label(card, text=item["text"], bg=INPUT, fg=TEXT, wraplength=width,
                         justify="left", anchor="w", font=("Microsoft YaHei UI", 10),
                         padx=10, pady=8).pack(fill="x")
                row = tk.Frame(card, bg=INPUT)
                row.pack(fill="x", padx=10, pady=(0, 8))
                tk.Button(row, text="忘掉", relief="flat", bg=INPUT, fg=DIM, bd=0,
                          font=("Microsoft YaHei UI", 9),
                          command=lambda i=item["id"]: self._memory_action(i, "forget")
                          ).pack(side="right")

        if not active and not pending:
            tk.Label(inner, text="还什么都没记住。\n多聊几句，她会在后台留意；\n"
                                 "或者直接跟她说「记住：…」。",
                     bg=BG, fg=DIM, justify="left", anchor="w",
                     font=("Microsoft YaHei UI", 10)).pack(fill="x", pady=12)

        expired = data.get("superseded") or []
        if expired:
            tk.Label(inner, text="已过期——被新事实替换，留着可查", bg=BG, fg=DIM,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(16, 4))
            for item in expired:
                tk.Label(inner, text=item["text"], bg=BG, fg=DIM, wraplength=width,
                         justify="left", anchor="w", font=("Microsoft YaHei UI", 9),
                         padx=10, pady=2).pack(fill="x")

        forgotten = data.get("forgotten") or []
        if forgotten:
            tk.Label(inner, text="最近删除——7 天内还能捡回来", bg=BG, fg=DIM,
                     font=("Microsoft YaHei UI", 9), anchor="w").pack(fill="x", pady=(16, 4))
            for item in forgotten:
                card = tk.Frame(inner, bg=INPUT)
                card.pack(fill="x", pady=3)
                tk.Label(card, text=item["text"], bg=INPUT, fg=DIM, wraplength=width,
                         justify="left", anchor="w", font=("Microsoft YaHei UI", 9),
                         padx=10, pady=6).pack(fill="x")
                row = tk.Frame(card, bg=INPUT)
                row.pack(fill="x", padx=10, pady=(0, 6))
                tk.Button(row, text="恢复", relief="flat", bg=INPUT, fg=TEXT, bd=0,
                          font=("Microsoft YaHei UI", 9),
                          command=lambda i=item["id"]: self._memory_action(i, "restore")
                          ).pack(side="right")

    def _memory_action(self, memory_id, action):
        def work():
            try:
                if action in ("confirm", "restore"):
                    request = Request(f"{CORE}/api/memories/{memory_id}", method="POST",
                                      data=json.dumps({"action": action}).encode(),
                                      headers=self.headers())
                else:
                    request = Request(f"{CORE}/api/memories/{memory_id}", method="DELETE",
                                      headers=self.headers())
                urlopen(request, timeout=20).close()
            except Exception as exc:
                self.events.put(("note", f"没成功：{exc}"))
            # 不能在这里直接刷新窗口——这是后台线程，tkinter 不接受。
            # 交给主线程的 drain 去做。
            self.events.put(("refresh_memory", None))
        threading.Thread(target=work, daemon=True).start()

    def set_memory_badge(self, count):
        """把待确认条数挂在「记忆」按钮上，不用敲命令也知道有事要处理。"""
        if self.memory_button is None:
            return
        try:
            if count:
                self.memory_button.config(text=f"记忆 {count}", fg="#ffd479")
            else:
                self.memory_button.config(text="记忆", fg=DIM)
        except tk.TclError:
            pass

    def _load_memories(self):
        try:
            with urlopen(Request(f"{CORE}/api/memories", headers=self.headers()), timeout=15) as response:
                data = json.loads(response.read() or b"{}")
        except Exception as exc:
            self.events.put(("note", f"记忆没读到：{exc}"))
            return
        active = data.get("active") or []
        pending = data.get("pending") or []
        self.memory_ids = []
        if not active and not pending:
            self.events.put(("note", "还什么都没记住。多聊几句，或者直接说「记住：…」。"))
            return
        if active:
            self.events.put(("note", "她记着的（这些才算数）："))
            for item in active:
                self.memory_ids.append(item["id"])
                self.events.put(("note", f"  {len(self.memory_ids)}. {item['text']}"))
        if pending:
            self.events.put(("note", "待确认——这些她还没敢当真，你点头才算："))
            for item in pending:
                self.memory_ids.append(item["id"])
                mark = "（你说过）" if item.get("explicit") else "（她猜的）"
                self.events.put(("note", f"  {len(self.memory_ids)}. {item['text']} {mark}"))
            self.events.put(("note", "  /收 编号 收下，/忘 编号 不要。"))

    def _act_on_memory(self, arg, action):
        if not self.memory_ids:
            self._load_memories()
        try:
            index = int(arg)
            memory_id = self.memory_ids[index - 1]
        except (ValueError, IndexError):
            self.events.put(("note", "给我一个列表里的编号，比如 /忘 2。先 /记忆 看一眼。"))
            return

        def work():
            try:
                if action == "confirm":
                    request = Request(f"{CORE}/api/memories/{memory_id}", method="POST",
                                      data=json.dumps({"action": "confirm"}).encode(),
                                      headers=self.headers())
                    urlopen(request, timeout=15).close()
                    self.events.put(("note", "记下了。"))
                else:
                    request = Request(f"{CORE}/api/memories/{memory_id}", method="DELETE",
                                      headers=self.headers())
                    urlopen(request, timeout=15).close()
                    self.events.put(("note", "忘了。"))
            except Exception as exc:
                self.events.put(("note", f"没成功：{exc}"))
            self._load_memories()
        threading.Thread(target=work, daemon=True).start()

    # ---------- 面板 ----------

    def open_panel(self):
        if self.panel is not None and self.panel.winfo_exists():
            self.panel.deiconify()
            self.panel.lift()
            if self.entry is not None:
                self.entry.focus_set()
            self.last_tick = time.time()
            threading.Thread(target=self.check_proactive, args=("panel_open",), daemon=True).start()
            return

        self.panel = tk.Toplevel(self.root)
        self.panel.title(self.name)
        self.panel.configure(bg=BG)
        self.panel.attributes("-topmost", True)
        self.panel.minsize(380, 420)

        # 用 grid 而不是 pack：聊天区的 Text 默认请求高度是 24 行，
        # 用 pack + expand 会把它先吃掉，输入行就落到 1x1、根本不会显示。
        self.panel.grid_rowconfigure(1, weight=1)
        self.panel.grid_columnconfigure(0, weight=1)

        header = tk.Frame(self.panel, bg=BG)
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 6))
        self.header_label = tk.Label(header, text=self.name, bg=BG, fg=TEXT,
                                     font=("Segoe UI", 15, "bold"))
        self.header_label.pack(side="left")
        self.status = tk.Label(header, text="", bg=BG, fg=DIM, font=("Microsoft YaHei UI", 9))
        self.status.pack(side="left", padx=8)
        self.memory_button = tk.Button(header, text="记忆", relief="flat", bg=BG, fg=DIM,
                                       activebackground=PANEL, bd=0,
                                       font=("Microsoft YaHei UI", 9),
                                       command=self.open_memory_window)
        self.memory_button.pack(side="right")
        tk.Button(header, text="简报", relief="flat", bg=BG, fg=DIM, activebackground=PANEL,
                  font=("Microsoft YaHei UI", 9), bd=0,
                  command=lambda: self.command("/简报")).pack(side="right", padx=(0, 6))

        text_frame = tk.Frame(self.panel, bg=PANEL)
        text_frame.grid(row=1, column=0, sticky="nsew", padx=16)
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)
        self.transcript = tk.Text(text_frame, bg=PANEL, fg=TEXT, wrap="word", relief="flat",
                                  height=6, width=20, padx=12, pady=10, bd=0,
                                  font=("Microsoft YaHei UI", 10), insertbackground=TEXT)
        scrollbar = tk.Scrollbar(text_frame, command=self.transcript.yview, width=10)
        self.transcript.configure(yscrollcommand=scrollbar.set)
        self.transcript.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.transcript.tag_configure("me", foreground=ACCENT, spacing1=8, spacing3=2)
        self.transcript.tag_configure("zero", foreground=TEXT, spacing1=8, spacing3=6)
        self.transcript.tag_configure("note", foreground=DIM, font=("Microsoft YaHei UI", 9),
                                      spacing1=2, spacing3=2)
        self.transcript.configure(state="disabled")

        bottom = tk.Frame(self.panel, bg=BG)
        bottom.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 14))
        bottom.grid_columnconfigure(0, weight=1)
        self.entry = tk.Entry(bottom, bg=INPUT, fg=TEXT, relief="flat", bd=6,
                              font=("Microsoft YaHei UI", 10), insertbackground=TEXT)
        self.entry.grid(row=0, column=0, sticky="ew", ipady=6)
        self.entry.bind("<Return>", lambda _: self.submit())
        self.entry.bind("<Escape>", lambda _: self.panel.withdraw())
        send_button = tk.Button(bottom, text="发送", relief="flat", bg="#33507c", fg=TEXT,
                                activebackground="#3d5f92", bd=0,
                                font=("Microsoft YaHei UI", 10), command=self.submit)
        send_button.grid(row=0, column=1, padx=(8, 0), ipady=5, ipadx=10)

        # 贴着球开，别飞到屏幕左上角
        self.panel.update_idletasks()
        width, height = 460, 560
        x = min(max(self.ball_x - width + 60, 0), self.root.winfo_screenwidth() - width - 10)
        y = min(max(self.ball_y - height + 40, 0), self.root.winfo_screenheight() - height - 40)
        self.panel.geometry(f"{width}x{height}+{x}+{y}")

        self.entry.focus_set()
        if not self.transcript.get("1.0", "end").strip():
            self.append(HELP, "note")
        self.last_tick = time.time()
        threading.Thread(target=self.check_proactive, args=("panel_open",), daemon=True).start()

    def append(self, text, tag, newline=True):
        if self.transcript is None or not self.transcript.winfo_exists():
            return
        self.transcript.configure(state="normal")
        # 流式输出没有结尾换行，直接插下一句会跟上一句粘在同一行。
        # 开新行之前先确认上一行真的结束了。
        if newline:
            content = self.transcript.get("1.0", "end-1c")
            if content and not content.endswith("\n"):
                self.transcript.insert("end", "\n")
        self.transcript.insert("end", text + ("\n" if newline else ""), tag)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def clear_transcript(self):
        if self.transcript is None or not self.transcript.winfo_exists():
            return
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")

    def show_confirm(self, action):
        if self.transcript is None or not self.transcript.winfo_exists():
            return
        self.append("要我动手吗？" + action.get("summary", ""), "note")
        bar = tk.Frame(self.panel, bg=BG)
        bar.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        tk.Button(bar, text="确认", relief="flat", bg="#7a5a22", fg="#ffe0bb", bd=0,
                  font=("Microsoft YaHei UI", 9),
                  command=lambda: self.decide(action["id"], "confirm", bar)).pack(side="left", padx=(0, 8))
        tk.Button(bar, text="算了", relief="flat", bg=INPUT, fg=TEXT, bd=0,
                  font=("Microsoft YaHei UI", 9),
                  command=lambda: self.decide(action["id"], "cancel", bar)).pack(side="left")
        self.panel.after(600000, bar.destroy)

    def decide(self, action_id, verb, bar):
        def work():
            request = Request(f"{CORE}/api/actions/{action_id}/{verb}", data=b"{}",
                              headers=self.headers(), method="POST")
            try:
                with urlopen(request, timeout=30) as response:
                    data = json.loads(response.read() or b"{}")
                if data.get("result"):
                    self.events.put(("note", data["result"].get("summary", "好了")))
                elif data.get("error"):
                    self.events.put(("note", data["error"]))
            except Exception as exc:
                self.events.put(("note", f"没成功：{exc}"))
        bar.destroy()
        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    Pet().root.mainloop()
