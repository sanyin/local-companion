"""眼睛：知道主人此刻在干什么。

只做**结构性**感知，不做像素识别：
  · 前台窗口是哪个程序
  · 在这个窗口待了多久
  · 键鼠空了多久
  · 今天各个程序累计用了多久

为什么不做截屏识图：那要再加载一个视觉模型，和 8B 抢那 8GB 显存，
代价是速度；而"你在同一个窗口改了一小时"这种信息，结构感知就够，
还不用碰屏幕内容。

隐私默认值：**进程名和时长会落盘，窗口标题只在内存里**。
标题里经常有文件名、聊天对象、搜索词，那些不该被记下来。
"""

import ctypes
import json
import threading
import time
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TZ = timezone(timedelta(hours=8))


# ---------- Win32 ----------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_QUERY_LIMITED_INFORMATION = 0x1000


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds():
    """系统多久没人动键鼠了。"""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        elapsed = int(_kernel32.GetTickCount()) - int(info.dwTime)
        return max(0.0, elapsed / 1000.0)
    except Exception:
        return 0.0


def foreground():
    """当前前台窗口的程序名和标题。拿不到就返回占位，不抛。"""
    unknown = {"process": "unknown.exe", "title": ""}
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return unknown
        length = _user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()

        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return {"process": unknown["process"], "title": title}

        handle = _kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return {"process": unknown["process"], "title": title}
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                name = Path(path.value).name or unknown["process"]
            else:
                name = unknown["process"]
        finally:
            _kernel32.CloseHandle(handle)
        return {"process": name, "title": title}
    except Exception:
        return unknown


# ---------- 观察者 ----------

def _today():
    return datetime.now(LOCAL_TZ).date().isoformat()


class DeskWatcher:
    """每 sample_seconds 采样一次，记下"哪个程序在前台、待了多久"。

    只落盘进程名和时长；窗口标题只在内存里，重启即忘。
    """

    def __init__(self, usage_path=None, sample_seconds=20,
                 probe=None, idle=None, clock=None):
        self.usage_path = Path(usage_path) if usage_path else None
        self.sample_seconds = max(1, int(sample_seconds))
        self._probe = probe or foreground
        self._idle = idle or idle_seconds
        self._clock = clock or time.time

        self._lock = threading.RLock()
        self._thread = None
        self._stop = threading.Event()

        self._process = ""
        self._title = ""
        self._since = None          # 当前窗口从什么时候开始在前台
        self._last_sample = None
        self._apps = {}             # process -> 秒
        self._day = _today()
        self._loaded = False
        self._dirty = False

    # ---------- 持久化 ----------

    def load(self):
        if self._loaded or not self.usage_path:
            return
        with self._lock:
            try:
                raw = json.loads(self.usage_path.read_text(encoding="utf-8") or "{}")
            except (FileNotFoundError, json.JSONDecodeError):
                raw = {}
            if isinstance(raw, dict) and raw.get("date") == _today():
                apps = raw.get("apps")
                if isinstance(apps, dict):
                    self._apps = {str(k): float(v) for k, v in apps.items()
                                  if isinstance(v, (int, float))}
            self._day = _today()
            self._loaded = True

    def save(self, force=False):
        if not self.usage_path:
            return
        with self._lock:
            if not force and not self._dirty:
                return
            payload = {
                "date": self._day,
                "apps": {name: round(seconds, 1) for name, seconds in self._apps.items()},
            }
            self.usage_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.usage_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.usage_path)
            self._dirty = False

    # ---------- 采样 ----------

    def sample(self, now=None):
        now = now or self._clock()
        self.load()
        with self._lock:
            if _today() != self._day:
                # 跨天：把昨天清零，只留今天的
                self._day = _today()
                self._apps = {}
                self._dirty = True

            state = self._probe() or {}
            process = (state.get("process") or "").lower()
            title = state.get("title") or ""

            # 前台有时候拿不到：隐藏的控制台、受保护的进程、桌面临时切换。
            # 这时候不能把用量记到 unknown 头上，也不能把显示改坏——
            # 保持上一次的判断，等下一次采样。
            blind = process in ("", "unknown.exe") and not title
            if blind and self._process not in ("", "unknown.exe"):
                process, title = self._process, self._title
            elif blind:
                process = "unknown.exe"

            if process != self._process:
                self._process = process
                self._since = now
            self._title = title

            # 只有"人真的在电脑前"的时间才算使用时长
            if self._idle() < 300 and self._last_sample is not None and process != "unknown.exe":
                delta = max(0.0, min(now - self._last_sample, self.sample_seconds * 3))
                self._apps[process] = self._apps.get(process, 0.0) + delta
                self._dirty = True
            self._last_sample = now
            return self.current()

    def _loop(self):
        while not self._stop.wait(self.sample_seconds):
            try:
                self.sample()
                self.save()
            except Exception:
                pass

    def start(self):
        self.load()
        self._last_sample = self._clock()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self.save(force=True)

    # ---------- 查询 ----------

    def current(self):
        with self._lock:
            now = self._clock()
            focus_minutes = ((now - self._since) / 60.0) if self._since else 0.0
            return {
                "process": self._process or "unknown.exe",
                "title": self._title,
                "focus_minutes": round(max(0.0, focus_minutes), 1),
                "idle_minutes": round(self._idle() / 60.0, 1),
            }

    def today(self, limit=5):
        with self._lock:
            ranked = sorted(self._apps.items(), key=lambda kv: kv[1], reverse=True)
            return [(name, round(seconds / 60.0, 1)) for name, seconds in ranked[:limit]]

    def describe(self):
        """说给主人听的一句话。"""
        state = self.current()
        if state["idle_minutes"] >= 5:
            return f"你已经离开电脑 {state['idle_minutes']:.0f} 分钟了。"
        title = state["title"]
        label = shorten_title(title) if title else pretty_app(state["process"])
        parts = [f"你正在 {label}"]
        if state["focus_minutes"] >= 1:
            parts.append(f"已经 {state['focus_minutes']:.0f} 分钟")
        text = "，".join(parts) + "。"
        usage = self.today(3)
        if usage:
            top = "、".join(f"{pretty_app(name)} {_duration(minutes)}" for name, minutes in usage)
            text += f"\n今天用得最多的是：{top}。"
        return text


def _duration(minutes):
    if minutes < 1:
        return "不到 1 分钟"
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


def shorten_title(title, limit=40):
    """窗口标题通常很长（"文件 - 应用 - 版本"），砍短一点。"""
    title = " ".join((title or "").split())
    if len(title) <= limit:
        return title
    return title[:limit - 1] + "…"


_FRIENDLY = {
    "code.exe": "VS Code", "chrome.exe": "Chrome", "msedge.exe": "Edge",
    "firefox.exe": "Firefox", "explorer.exe": "资源管理器", "notepad.exe": "记事本",
    "wps.exe": "WPS", "winword.exe": "Word", "excel.exe": "Excel",
    "powerpnt.exe": "PowerPoint", "wechat.exe": "微信", "qq.exe": "QQ",
    "dingtalk.exe": "钉钉", "feishu.exe": "飞书", "terminal.exe": "终端",
    "windowsterminal.exe": "终端", "pwsh.exe": "PowerShell", "cmd.exe": "命令行",
    "python.exe": "Python", "idea64.exe": "IDEA", "pycharm64.exe": "PyCharm",
    "typora.exe": "Typora", "obsidian.exe": "Obsidian",
}


def pretty_app(process):
    name = (process or "").lower()
    return _FRIENDLY.get(name) or (name.rsplit(".", 1)[0].title() if name else "未知程序")
