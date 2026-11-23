"""手。

三个原则，全部写在代码里，不写在提示词里：
  1. 只能碰**授权目录**里的文件。越界直接拒绝，不给模型商量的余地。
  2. 读是安全的，写和删**必须主人点头**。确认是一次性的、会过期的凭据，
     模型自己没法跳过这一步——它根本没有那个能力。
  3. 每个工具的返回都自带出处。出处由这里生成，模型只负责转述，
     所以它没有机会编造一个不存在的引用。
"""

import difflib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from reminder import LOCAL_TZ
import web

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "AppData", ".cache", "site-packages"}
SKIP_SUFFIXES = {".exe", ".dll", ".so", ".dylib", ".pyc", ".zip", ".7z", ".rar", ".gguf",
                 ".mp4", ".mkv", ".mp3", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".db", ".sqlite3"}
MAX_FILE_BYTES = 256 * 1024
PENDING_TTL_SECONDS = 600

# 她自己的私人物品：令牌、配置、记忆库。就算主人把 . 整个授权了，
# 这些也不许读、不许写、不许搜——「能碰的目录」不该等于「什么都能碰」。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {"memory"}
PRIVATE_FILES = {".zero_token", "config.json"}

# 搜索是她唯一一条不需要点头就能把数据送出机器的通道，所以单独留一份账。
SEARCH_LOG = PROJECT_ROOT / "logs" / "searches.jsonl"
MAX_QUERY_CHARS = 200
MAX_PENDING_TASKS = 3


class ToolError(Exception):
    pass


class ToolResult:
    def __init__(self, content, citations=None, summary=None, ok=True):
        self.ok = ok
        self.content = content
        self.citations = citations or []
        self.summary = summary or content

    def for_model(self):
        return self.content

    def as_dict(self):
        return {"ok": self.ok, "content": self.content, "citations": self.citations, "summary": self.summary}


def _human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024


class ToolBox:
    def __init__(self, roots=None, reminders=None, city="上海", watcher=None, tasks=None):
        self.set_roots(roots or [])
        self.reminders = reminders or []
        self.city = city or "上海"
        self.watcher = watcher
        self.tasks = tasks
        self._pending = {}

    # ---------- 授权 ----------

    def set_roots(self, roots):
        resolved = []
        for root in roots:
            try:
                resolved.append(Path(root).expanduser().resolve())
            except OSError:
                continue
        self.roots = resolved

    def has_roots(self):
        return bool(self.roots)

    def resolve(self, raw_path):
        """把路径变成绝对路径，并确认它在授权目录里。"""
        if not self.roots:
            raise ToolError("还没有授权任何文件夹。先让主人授权一个目录，否则我什么都看不到。")
        try:
            target = Path(raw_path).expanduser()
            if not target.is_absolute():
                target = self.roots[0] / target
            target = target.resolve()
        except (OSError, ValueError) as exc:
            raise ToolError(f"这个路径我读不了：{exc}") from None
        for root in self.roots:
            if target == root or root in target.parents:
                if self._is_private(target):
                    raise ToolError("那个文件是她的私人物品（令牌/配置/记忆），不能读也不能改。")
                return target
        raise ToolError(f"{target} 不在授权范围里。我能碰的只有：" + "、".join(str(r) for r in self.roots))

    @staticmethod
    def _is_private(target):
        try:
            rel = target.relative_to(PROJECT_ROOT)
        except ValueError:
            return False
        if rel.parts and rel.parts[0] in PRIVATE_DIRS:
            return True
        return target.name in PRIVATE_FILES

    # ---------- 工具定义 ----------

    def specs(self):
        return [
            {"type": "function", "function": {
                "name": "get_time",
                "description": "读取当前真实日期和时间。涉及今天/现在/星期几时必须调用，不要猜。",
                "parameters": {"type": "object", "properties": {}, "required": []}}},
            {"type": "function", "function": {
                "name": "list_dir",
                "description": "列出一个授权目录里的文件。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "目录路径"}}, "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "read_file",
                "description": "读取一个文本文件的内容。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "max_chars": {"type": "integer", "description": "最多读多少字，默认 4000"}},
                    "required": ["path"]}}},
            {"type": "function", "function": {
                "name": "search_files",
                "description": "在授权目录里按文件名或内容搜索，返回匹配的文件和命中行。",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "要搜的词"},
                    "root": {"type": "string", "description": "在哪个目录下搜，可省略"},
                    "limit": {"type": "integer", "description": "最多返回几条，默认 10"}},
                    "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "add_reminder",
                "description": "建一个提醒。when 可以是 +30m、09:00、或 2026-11-16T09:00。",
                "parameters": {"type": "object", "properties": {
                    "text": {"type": "string", "description": "提醒内容"},
                    "when": {"type": "string", "description": "什么时候提醒"},
                    "repeat": {"type": "string", "enum": ["none", "daily", "weekdays"]}},
                    "required": ["text", "when"]}}},
            {"type": "function", "function": {
                "name": "get_weather",
                "description": "查天气。问今天/明天冷不冷、要不要带伞、下不下雨时必须用。"
                               "问明天就传 day=明天，不要拿今天的数据去推明天。",
                "parameters": {"type": "object", "properties": {
                    "city": {"type": "string", "description": "城市名，省略就用主人常驻的城市"},
                    "day": {"type": "string", "enum": ["今天", "明天"],
                            "description": "问哪一天，默认今天"}},
                    "required": []}}},
            {"type": "function", "function": {
                "name": "get_headlines",
                "description": "拿今天发生的大事（时事要闻）。主人问今天有什么新闻、发生什么了时用。",
                "parameters": {"type": "object", "properties": {
                    "limit": {"type": "integer", "description": "要几条，默认 6"}},
                    "required": []}}},
            {"type": "function", "function": {
                "name": "search_web",
                "description": "联网搜索。问需要最新/外部信息的问题时用，比如某个东西是什么、怎么解决。",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "description": "搜索词"},
                    "limit": {"type": "integer", "description": "要几条，默认 5"}},
                    "required": ["query"]}}},
            {"type": "function", "function": {
                "name": "what_am_i_doing",
                "description": "看主人此刻在电脑上干什么：前台程序、待了多久、今天各个程序用了多长时间。"
                               "他要你提醒久坐、或者问「我在忙什么」时用。",
                "parameters": {"type": "object", "properties": {}, "required": []}}},
            {"type": "function", "function": {
                "name": "spawn_task",
                "description": "把一件活派到后台去做，你可以继续跟主人说话。"
                               "适合比较久、不需要立刻回话的事（查资料、整理一批文件、写一段东西）。"
                               "做完会通知主人。不要用来回答他正在等的那句话。",
                "parameters": {"type": "object", "properties": {
                    "title": {"type": "string", "description": "一句话说明这是什么活，给主人看的"},
                    "instruction": {"type": "string", "description": "要做什么，写清楚，后台的人看不到你们的对话"}},
                    "required": ["title", "instruction"]}}},
            {"type": "function", "function": {
                "name": "write_file",
                "description": "写入或覆盖一个文本文件。需要主人确认。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "文件内容"}},
                    "required": ["path", "content"]}}},
            {"type": "function", "function": {
                "name": "delete_file",
                "description": "删除一个文件。需要主人确认。",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string", "description": "要删的文件路径"}},
                    "required": ["path"]}}},
        ]

    PERMISSION = {
        "get_time": "safe", "list_dir": "safe", "read_file": "safe",
        "search_files": "safe", "add_reminder": "safe",
        "get_weather": "safe", "get_headlines": "safe", "search_web": "safe",
        "what_am_i_doing": "safe",
        "spawn_task": "safe",
        "write_file": "confirm", "delete_file": "confirm",
    }

    def permission(self, name):
        return self.PERMISSION.get(name, "confirm")

    # ---------- 安全工具：直接执行 ----------

    def invoke(self, name, args):
        if self.permission(name) != "safe":
            raise ToolError(f"{name} 需要主人确认，不能直接执行。")
        handler = {
            "get_time": self._get_time,
            "list_dir": self._list_dir,
            "read_file": self._read_file,
            "search_files": self._search_files,
            "add_reminder": self._add_reminder,
            "get_weather": self._get_weather,
            "get_headlines": self._get_headlines,
            "search_web": self._search_web,
            "what_am_i_doing": self._what_am_i_doing,
            "spawn_task": self._spawn_task,
        }.get(name)
        if handler is None:
            raise ToolError(f"没有这个工具：{name}")
        try:
            return handler(args or {})
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"{name} 执行失败：{exc}") from None

    def _get_time(self, args):
        moment = datetime.now(LOCAL_TZ)
        weekdays = "一二三四五六日"
        text = f"{moment.strftime('%Y年%m月%d日')} 星期{weekdays[moment.weekday()]} {moment.strftime('%H:%M')}"
        return ToolResult(text, citations=[f"系统时间 {text}"])

    def _list_dir(self, args):
        target = self.resolve(args.get("path", ""))
        if not target.is_dir():
            raise ToolError(f"{target} 不是一个目录。")
        entries = []
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:60]:
            if self._is_private(child.resolve()):
                continue
            tag = "/" if child.is_dir() else ""
            size = "" if child.is_dir() else f"  {_human_size(child.stat().st_size)}"
            entries.append(f"{child.name}{tag}{size}")
        body = "\n".join(entries) or "（空目录）"
        return ToolResult(f"{target}\n{body}", citations=[f"目录 {target}"])

    def _read_file(self, args):
        target = self.resolve(args.get("path", ""))
        if not target.is_file():
            raise ToolError(f"{target} 不是一个文件。")
        size = target.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ToolError(f"文件太大（{_human_size(size)}），我没有办法整个读完。")
        raw = target.read_bytes()
        for encoding in ("utf-8", "gbk", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            raise ToolError(f"{target} 看起来不是文本文件。")
        limit = int(args.get("max_chars") or 4000)
        clipped = text[:limit]
        suffix = f"\n…（还有 {len(text) - limit} 字没显示）" if len(text) > limit else ""
        return ToolResult(f"{target} 的内容：\n{clipped}{suffix}", citations=[f"文件 {target}"])

    def _search_files(self, args):
        """找东西。这是私人助理最日常的活，所以按「像不像你要找的那个」排序，
        而不是按目录遍历的顺序——排错了她就会把上周改的文件埋在一堆旧文件后面。
        """
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError("搜索需要关键词。")
        base = self.resolve(args.get("root") or (self.roots[0] if self.roots else ""))
        limit = max(1, min(int(args.get("limit") or 8), 20))

        words = [w.lower() for w in query.split() if w.strip()] or [query.lower()]
        now = time.time()
        candidates = []
        scanned = 0
        close_names = []

        for path in base.rglob("*"):
            if scanned > 6000:
                break
            try:
                parts = path.relative_to(base).parts
            except ValueError:
                continue
            if any(part in SKIP_DIRS for part in parts):
                continue
            if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
                continue
            if self._is_private(path.resolve()):
                continue
            scanned += 1

            name = path.name.lower()
            name_hits = sum(1 for w in words if w in name)
            if len(close_names) < 40:
                close_names.append(path.name)

            snippet = ""
            content_hits = 0
            if name_hits < len(words):
                try:
                    if path.stat().st_size > MAX_FILE_BYTES:
                        continue
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                lowered = text.lower()
                content_hits = sum(lowered.count(w) for w in words)
                for number, line in enumerate(text.splitlines(), 1):
                    if all(w in line.lower() for w in words):
                        snippet = f":{number}  {line.strip()[:110]}"
                        break

            if name_hits == 0 and content_hits == 0:
                continue
            try:
                age_days = (now - path.stat().st_mtime) / 86400
            except OSError:
                age_days = 999

            # 文件名命中远重于内容命中；最近改过的加权
            score = name_hits * 100 + min(content_hits, 20) * 3
            if age_days <= 7:
                score += 40
            elif age_days <= 30:
                score += 15
            candidates.append((score, age_days, str(path), snippet))

        if not candidates:
            guess = difflib.get_close_matches(query, close_names, n=3, cutoff=0.5)
            tail = f"\n要不要试试：{'、'.join(guess)}" if guess else ""
            return ToolResult(f"在 {base} 下没有找到「{query}」。{tail}",
                              citations=[f"搜索 {base}"])

        candidates.sort(key=lambda row: (-row[0], row[1]))
        lines = []
        for score, age_days, path, snippet in candidates[:limit]:
            when = "今天" if age_days < 1 else f"{int(age_days)} 天前"
            lines.append(f"{path}  （{when}改过）{snippet}")
        more = f"\n（还有 {len(candidates) - limit} 个没列出来）" if len(candidates) > limit else ""
        return ToolResult(f"找到 {len(candidates)} 个，最像的排前面：\n" + "\n".join(lines) + more,
                          citations=[f"搜索 {base}"])

    def _add_reminder(self, args):
        if not self.reminders:
            raise ToolError("提醒存储没有接上。")
        text = args.get("text")
        when = args.get("when")
        repeat = args.get("repeat") or "none"
        item = self.reminders.add(text, when, repeat=repeat)
        moment = datetime.fromisoformat(item["next_fire_at"])
        readable = moment.strftime("%m月%d日 %H:%M")
        return ToolResult(f"提醒已建好：{readable} —— {item['text']}", citations=[f"提醒 {readable}"])

    # ---------- 联网 ----------

    def _get_weather(self, args):
        city = (args.get("city") or self.city or "上海").strip()
        day = args.get("day") or "今天"
        need_tomorrow = "明" in str(day) or "明天" in str(day)
        try:
            text = web.weather_text(city, days=2 if need_tomorrow else 1)
        except web.WebError as exc:
            raise ToolError(f"天气没查到：{exc}") from None
        return ToolResult(text, citations=[f"天气 {city} · open-meteo"])

    def _get_headlines(self, args):
        limit = int(args.get("limit") or 6)
        try:
            items = web.headlines(limit=limit)
        except web.WebError as exc:
            raise ToolError(f"要闻没拿到：{exc}") from None
        body = "\n".join(f"{i}. {item}" for i, item in enumerate(items, 1))
        return ToolResult("今天的事：\n" + body, citations=["要闻 · 60s-api"])

    def _search_web(self, args):
        query = (args.get("query") or "").strip()
        limit = int(args.get("limit") or 5)
        if not query:
            raise ToolError("搜索需要关键词。")
        if len(query) > MAX_QUERY_CHARS:
            raise ToolError(f"搜索词太长了（{len(query)} 字）。她应该先把问题缩短，而不是把一整段话发出去。")
        self._log_search(query)
        try:
            results = web.search(query, limit=limit)
        except web.WebError as exc:
            raise ToolError(f"搜索没成功：{exc}") from None
        lines = []
        for i, item in enumerate(results, 1):
            snippet = f"\n   {item['snippet']}" if item.get("snippet") else ""
            lines.append(f"{i}. {item['title']}{snippet}")
        return ToolResult("搜到这些：\n" + "\n".join(lines), citations=[f"搜索「{query}」"])

    @staticmethod
    def _log_search(query):
        try:
            SEARCH_LOG.parent.mkdir(parents=True, exist_ok=True)
            with SEARCH_LOG.open("a", encoding="utf-8") as handle:
                record = {"ts": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                          "query": query[:MAX_QUERY_CHARS]}
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _what_am_i_doing(self, args):
        """看一眼主人此刻在干什么。没有观察者就直说看不到。"""
        if self.watcher is None:
            raise ToolError("我没有在看着你的桌面（桌面感知没开）。")
        try:
            return ToolResult(self.watcher.describe(), citations=["桌面感知 · 本机"])
        except Exception as exc:
            raise ToolError(f"看不了：{exc}") from None

    def _spawn_task(self, args):
        """把活派到后台。排队满了就直说，别让它无限堆。"""
        if self.tasks is None:
            raise ToolError("后台队列没接上。")
        instruction = (args.get("instruction") or "").strip()
        title = (args.get("title") or instruction[:30]).strip()
        if not instruction:
            raise ToolError("没说要做什么。")
        pending = [t for t in self.tasks.list() if t["status"] in ("pending", "running")]
        if len(pending) >= MAX_PENDING_TASKS:
            raise ToolError(f"后台已经有 {len(pending)} 件活在排队，先等它们做完。")
        task = self.tasks.submit(title, instruction)
        return ToolResult(
            f"已经排进后台：{task['title']}。做完我会说一声。",
            citations=[f"后台任务 {task['id']}"],
        )

    # ---------- 危险工具：先排队，等主人点头 ----------

    def describe(self, name, args):
        """给主人看的确认文案。必须包含具体路径和后果，不能只说「执行一个操作」。"""
        args = args or {}
        if name == "delete_file":
            target = self.resolve(args.get("path", ""))
            if not target.exists():
                raise ToolError(f"{target} 不存在。")
            kind = "目录" if target.is_dir() else f"文件，{_human_size(target.stat().st_size)}"
            return f"删除{kind}：{target}\n（不可恢复，不进回收站）"
        if name == "write_file":
            target = self.resolve(args.get("path", ""))
            content = args.get("content") or ""
            action = "覆盖" if target.exists() else "新建"
            return f"{action}文件：{target}\n写入 {len(content)} 字"
        if name == "search_web":
            query = (args.get("query") or "").strip()
            return f"联网搜索：{query}\n（搜索词里带着你记过的东西，先让你过一眼）"
        raise ToolError(f"{name} 不需要确认，或者不存在。")

    def stage(self, name, args, force=False):
        """把危险操作挂起，返回一张待确认的凭据。

        force=True 用于「本来安全、但这次可疑」的操作（目前只有搜索）：
        搜索不需要授权，但如果搜索词像是从记忆里拼出来的，就得先问一句。
        """
        if not force and self.permission(name) != "confirm":
            raise ToolError(f"{name} 是安全工具，直接执行即可。")
        summary = self.describe(name, args)
        # 以前用 len(pending)+1 拼 id：cancel 掉一条之后，新操作的 id 会和
        # 还挂在队里的那条撞上（b 被 c 覆盖，点确认执行的是另一个文件）。
        action_id = f"act_{uuid.uuid4().hex[:10]}"
        self._pending[action_id] = {
            "id": action_id, "name": name, "args": args or {},
            "summary": summary, "created_at": time.time(),
        }
        return dict(self._pending[action_id])

    def cancel(self, action_id):
        return self._pending.pop(action_id, None) is not None

    def execute(self, action_id):
        """主人点了确认，才真正动手。凭据一次性。"""
        # 先检查、再 pop：过期的操作留在队列里，还能被 cancel 掉；
        # 先 pop 的话，超时一次就再也点不动了，看起来像"已经执行过"。
        action = self._pending.get(action_id)
        if action is None:
            raise ToolError("这个操作已经失效了（超过十分钟，或者已经执行过）。")
        if time.time() - action["created_at"] > PENDING_TTL_SECONDS:
            raise ToolError("这个操作等太久了，已经作废。要我重来一次吗？")
        self._pending.pop(action_id, None)
        name, args = action["name"], action["args"]
        if name == "search_web":
            return self._search_web(args)
        if name == "delete_file":
            target = self.resolve(args.get("path", ""))
            if target.is_dir():
                raise ToolError("我不删目录。要删的话，请你自己在资源管理器里来一下。")
            target.unlink()
            return ToolResult(f"已删除 {target}", citations=[f"删除 {target}"])
        if name == "write_file":
            target = self.resolve(args.get("path", ""))
            target.parent.mkdir(parents=True, exist_ok=True)
            content = args.get("content") or ""
            target.write_text(content, encoding="utf-8")
            return ToolResult(f"已写入 {target}（{len(content)} 字）", citations=[f"写入 {target}"])
        raise ToolError(f"不认识的待确认操作：{name}")

    def pending(self):
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v["created_at"] <= PENDING_TTL_SECONDS}
        return [dict(v) for v in self._pending.values()]
