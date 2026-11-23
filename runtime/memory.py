"""记忆：让本地助理真的记得你。

两层：
  MemoryStore      —— 长期记忆。区分「主人亲口说过」和「她自己猜的」。
  ConversationLog  —— 对话流水。重启不丢，也是以后做 LoRA 的语料。

一条记忆的字段：
  id / text / kind / source / status / created_at / last_used / pinned
  source: user_explicit（主人亲口说的） | inferred（她自己猜的，等确认）
  status: active（生效） | pending（候选，等主人点头）
"""

import json
import math
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TZ = timezone(timedelta(hours=8))
MAX_MEMORY_CHARS = 240

# 进提示词的记忆条数上限。以前是 40 条按插入序截断——超过 40 条以后
# 新记的事永远进不去（追加在末尾正好被切掉）。现在按相关度挑。
DEFAULT_PROMPT_LIMIT = 15
# 相关度打分里，时间衰减的半衰期。两周前的偏好还该记得，两个月前的
# 一次性事件就不该抢位置了。
RECENT_HALF_LIFE_DAYS = 14


def _now():
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def _new_id(prefix="mem"):
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _is_legacy(item):
    """老格式是 {'text': ...}，没有 id/source/status。"""
    return isinstance(item, dict) and "text" in item and "id" not in item


def _grams(text):
    """把一句话切成字符 2-gram。中文不用分词也能做相关性，
    代价是偶尔把「上海」和「海上」算成相似——够用了。"""
    text = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    if len(text) < 2:
        return {text} if text else set()
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _overlap(query_grams, text_grams):
    """查询命中率，按记忆长度开方归一化（长句子不该天然占便宜），封顶 1。"""
    if not query_grams or not text_grams:
        return 0.0
    return min(1.0, len(query_grams & text_grams) / math.sqrt(len(text_grams)))


def _decay(stamp, now=None, half_life_days=RECENT_HALF_LIFE_DAYS):
    if not stamp:
        return 0.0
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0.0
    now = now or datetime.now(LOCAL_TZ)
    age_days = max(0.0, (now - moment).total_seconds() / 86400)
    return 0.5 ** (age_days / half_life_days)


def _default_importance(kind, source):
    """没有明确打分时的兜底。身份/偏好比一次性事件更该长期占位置。"""
    if kind == "preference":
        return 4
    if kind == "event":
        return 3
    return 4 if source == "user_explicit" else 3


def _char_ngrams(text, size):
    text = " ".join((text or "").split()).casefold()
    if len(text) < size:
        return {text} if text else set()
    return {text[i:i + size] for i in range(len(text) - size + 1)}



class MemoryStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rejected_path = self.path.with_name("rejected.json")
        self.prefs_path = self.path.with_name("preferences.jsonl")
        self.journal_path = self.path.with_name("journal.jsonl")
        self._lock = threading.RLock()
        self._items = self._load()
        self._rejected = self._load_rejected()

    # ---------- 读写 ----------

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8") or "[]")
        except (FileNotFoundError, json.JSONDecodeError):
            return []
        if isinstance(raw, dict):
            raw = raw.get("memories") or []
        items = []
        for item in raw:
            if not isinstance(item, dict) or not item.get("text"):
                continue
            if _is_legacy(item):
                items.append({
                    "id": _new_id(), "text": item["text"], "kind": "fact",
                    "source": "user_explicit", "status": "active",
                    "created_at": item.get("created_at") or _now(),
                    "last_used": None, "pinned": bool(item.get("pinned", False)),
                    "explicit": True, "importance": 3, "evidence": None,
                    "supersedes": [], "valid_from": item.get("created_at") or _now(),
                    "valid_to": None, "superseded_by": None, "approved_at": None,
                })
            else:
                item.setdefault("kind", "fact")
                item.setdefault("source", "user_explicit")
                item.setdefault("status", "active")
                item.setdefault("created_at", _now())
                item.setdefault("last_used", None)
                item.setdefault("pinned", False)
                item.setdefault("explicit", item.get("source") == "user_explicit")
                item.setdefault("importance", 3)
                item.setdefault("evidence", None)
                item.setdefault("supersedes", [])
                item.setdefault("valid_from", item.get("created_at"))
                item.setdefault("valid_to", None)
                item.setdefault("superseded_by", None)
                item.setdefault("approved_at", None)
                items.append(item)
        return items

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def _load_rejected(self):
        try:
            raw = json.loads(self.rejected_path.read_text(encoding="utf-8") or "[]")
        except (FileNotFoundError, json.JSONDecodeError):
            return set()
        return {str(item) for item in raw if item} if isinstance(raw, list) else set()

    def _save_rejected(self):
        # 只留最近一批，别让这个名单无限长
        recent = list(self._rejected)[-200:]
        self._rejected = set(recent)
        tmp = self.rejected_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.rejected_path)

    def _log_preference(self, item, decision):
        """「收下 / 不要」就是一对偏好样本：将来训抽取器、训开口时机都用得上。

        只记判断本身，不记原话；原话在 conversations.jsonl 里，要配对时按
        session + ts 去找。
        """
        try:
            record = {
                "ts": _now(), "memory_id": item.get("id"), "decision": decision,
                "text": item.get("text"), "kind": item.get("kind"),
                "explicit": bool(item.get("explicit")), "source": item.get("source"),
                "importance": item.get("importance"),
                "supersedes": item.get("supersedes") or [],
                "has_evidence": bool(item.get("evidence")),
            }
            with self.prefs_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _journal(self, event, item, extra=None):
        """追加一条不可变事件。memories.json 是投影，journal 是账本。

        这样"她什么时候记住/忘了什么"可查，误删也有东西可恢复；
        journal 损坏最多丢审计，不影响记忆本身。
        """
        record = {"ts": _now(), "event": event, "id": item.get("id"),
                  "item": dict(item)}
        if extra:
            record.update(extra)
        try:
            with self.journal_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _journal_records(self):
        if not self.journal_path.exists():
            return []
        records = []
        for line in self.journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    # ---------- 查询 ----------

    def all(self):
        with self._lock:
            return [dict(item) for item in self._items]

    def active(self):
        with self._lock:
            return [dict(item) for item in self._items if item.get("status") == "active"]

    def pending(self):
        with self._lock:
            return [dict(item) for item in self._items if item.get("status") == "pending"]

    def superseded(self):
        """被新事实替换掉的旧条目。留着可查，但不进提示词。"""
        with self._lock:
            return [dict(item) for item in self._items if item.get("status") == "superseded"]

    def find(self, memory_id):
        with self._lock:
            for item in self._items:
                if item["id"] == memory_id:
                    return dict(item)
        return None

    def similar_active(self, text, limit=5):
        """跟这句话最像的已生效记忆。抽取时用来判断「是不是在更新旧事实」。

        只按字面相似不够：『我搬到北京了』和『主人住在上海』一个 2-gram
        都不重合，可它们说的是同一件事。所以相似的不够时，用最近的记忆补齐
        ——搬家、换工作这类更新，旧事实通常就是最近记下的那条。
        """
        query = _grams(text)
        with self._lock:
            # 口味单独摆一块，别混进「你记得的」——那一块是事实，
            # 同一句话出现在两处会让她以为你说了两遍。
            items = [dict(item) for item in self._items
                     if item.get("status") == "active" and item.get("kind") != "style"]
        scored = [(_overlap(query, _grams(item["text"])), item) for item in items]
        scored = [pair for pair in scored if pair[0] > 0.12]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        picked = [item for _, item in scored[:limit]]
        if len(picked) < limit:
            seen = {item["id"] for item in picked}
            recent = sorted(items, key=lambda item: item.get("created_at") or "", reverse=True)
            for item in recent:
                if item["id"] in seen:
                    continue
                picked.append(item)
                seen.add(item["id"])
                if len(picked) >= limit:
                    break
        return picked

    def contains_snippet(self, text, min_len=8):
        """这句话里有没有整段记忆。

        她唯一一条不经确认就能把数据送出机器的通道是 search_web。
        如果搜索词里出现了记忆原文，那多半不是她想搜，是有人让她搜。
        """
        haystack = _char_ngrams(text, min_len)
        if not haystack:
            return None
        with self._lock:
            # 口味单独摆一块，别混进「你记得的」——那一块是事实，
            # 同一句话出现在两处会让她以为你说了两遍。
            items = [dict(item) for item in self._items
                     if item.get("status") == "active" and item.get("kind") != "style"]
        for item in items:
            if _char_ngrams(item["text"], min_len) & haystack:
                return item["text"]
        return None

    # ---------- 写入 ----------

    def add(self, text, kind="fact", source="user_explicit", status="active", pinned=False,
            explicit=True, importance=None, evidence=None, supersedes=None):
        text = " ".join((text or "").split()).strip()
        if not text:
            return None
        text = text[:MAX_MEMORY_CHARS]
        with self._lock:
            # 主人说"不要"的那条，别再拿出来烦他
            if source != "user_explicit" and text in self._rejected:
                return None
            # 去重：同一个意思不重复存。但"已过期"的旧条目不算重复——
            # 他后来又搬回上海，那是新的断言，得重新走一次确认。
            exact = next((item for item in self._items if item["text"] == text), None)
            if exact is not None and exact.get("status") != "superseded":
                if status == "active" and exact.get("status") != "active":
                    exact["status"] = "active"
                    exact["approved_at"] = _now()
                    self._save()
                    self._journal("reactivate", exact)
                return dict(exact)
            if exact is not None and exact.get("status") == "superseded" and status == "active":
                # 直接命令（/记）可以把它复活；候选不行，候选要重新确认。
                stamp = _now()
                exact.update(status="active", valid_from=stamp, valid_to=None,
                             superseded_by=None, approved_at=stamp)
                self._save()
                self._journal("reactivate", exact)
                return dict(exact)
            stamp = _now()
            item = {
                "id": _new_id(), "text": text, "kind": kind, "source": source,
                "status": status, "created_at": stamp, "last_used": None, "pinned": pinned,
                "explicit": bool(explicit),
                "importance": int(importance if importance is not None else _default_importance(kind, source)),
                "evidence": evidence,
                "supersedes": [str(i) for i in (supersedes or [])][:3],
                "valid_from": stamp, "valid_to": None, "superseded_by": None,
                "approved_at": None,
            }
            self._items.append(item)
            self._save()
            self._journal("add", item)
            return dict(item)

    def confirm(self, memory_id):
        """主人点头，候选记忆转为正式记忆；它声明要替换的旧条目同时失效。

        注意：这里不再把 source 改成 user_explicit —— 「她猜的、你批准了」
        和「你亲口说的」是两种东西，混成一种就没法审计了。
        """
        with self._lock:
            target = next((item for item in self._items if item["id"] == memory_id), None)
            if target is None:
                return None
            was_pending = target.get("status") == "pending"
            stamp = _now()
            target["status"] = "active"
            target["approved_at"] = stamp
            target["valid_from"] = stamp
            superseded = []
            for old_id in target.get("supersedes") or []:
                old = next((item for item in self._items if item["id"] == old_id), None)
                if old is None or old["id"] == target["id"] or old.get("status") != "active":
                    continue
                old["status"] = "superseded"
                old["valid_to"] = stamp
                old["superseded_by"] = target["id"]
                superseded.append(old["id"])
            self._save()
            self._journal("confirm", target, {"superseded": superseded})
            if was_pending:
                self._log_preference(target, "accept")
            return dict(target)

    def pin(self, memory_id, pinned=True):
        with self._lock:
            for item in self._items:
                if item["id"] == memory_id:
                    item["pinned"] = bool(pinned)
                    self._save()
                    return dict(item)
        return None

    def forget(self, memory_id):
        """主人让你忘，就忘。

        从 memories.json 里真删，但 journal 里留一份快照——误点「忘掉」还有
        7 天可恢复。7 天后再清掉快照，才是真的没了。
        """
        with self._lock:
            target = next((item for item in self._items if item["id"] == memory_id), None)
            if target is None:
                return False
            self._items = [item for item in self._items if item["id"] != memory_id]
            if target.get("status") != "active":
                # 只对没被采纳的候选留记号。正式记忆删了就是删了，
                # 以后再提到、再说一次，那是新的对话，该重新记得。
                self._rejected.add(target["text"])
                self._save_rejected()
            self._save()
            self._journal("forget", target)
            if target.get("status") == "pending":
                # 这是一次 "不要"，是偏好数据里的负样本。
                self._log_preference(target, "reject")
            return True

    def forgotten(self, days=7):
        """最近 N 天被删掉、还没恢复的记忆（journal 里的快照）。"""
        cutoff = datetime.now(LOCAL_TZ) - timedelta(days=days)
        with self._lock:
            current = {item["id"] for item in self._items}
            records = self._journal_records()
        found = {}
        for record in reversed(records):        # 最近的先看
            if record.get("event") != "forget":
                continue
            item = record.get("item") or {}
            memory_id = item.get("id") or record.get("id")
            if not memory_id or memory_id in current or memory_id in found:
                continue
            try:
                stamp = datetime.fromisoformat(record.get("ts") or "")
            except ValueError:
                continue
            if stamp < cutoff:
                continue
            found[memory_id] = item
        return list(found.values())

    def restore(self, memory_id):
        """把误删的那条放回去。恢复的是快照，不是重新抽取。"""
        with self._lock:
            if any(item["id"] == memory_id for item in self._items):
                return None
            snapshot = next((item for item in self.forgotten() if item.get("id") == memory_id), None)
            if snapshot is None:
                return None
            restored = dict(snapshot)
            restored["valid_to"] = None
            restored["superseded_by"] = None
            self._items.append(restored)
            if restored.get("status") == "pending":
                self._rejected.discard(restored.get("text") or "")
                self._save_rejected()
            self._save()
            self._journal("restore", restored)
            return dict(restored)

    def export_data(self):
        """整份记忆导出：主人随时可以把东西搬走。"""
        with self._lock:
            return {
                "exported_at": _now(),
                "memories": [dict(item) for item in self._items],
                "rejected": sorted(self._rejected),
            }

    def touch(self, memory_id):
        self.touch_many([memory_id])

    def touch_many(self, memory_ids):
        """把「这一轮真的用到了」记下来，供下次排序。一次落盘，别写 N 次。"""
        wanted = set(memory_ids or [])
        if not wanted:
            return
        stamp = _now()
        with self._lock:
            changed = False
            for item in self._items:
                if item["id"] in wanted:
                    item["last_used"] = stamp
                    changed = True
            if changed:
                self._save()

    # ---------- 进提示词 ----------

    def style_block(self, limit=8):
        """他确认过的口味。跟事实分开摆，因为这一块是「怎么说话」，不是「记了什么」。"""
        with self._lock:
            items = [dict(item) for item in self._items
                     if item.get("status") == "active" and item.get("kind") == "style"]
        if not items:
            return ""
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        lines = [f"- {item['text']}" for item in items[:limit]]
        return "\n".join(lines)

    def prompt_block(self, text="", limit=DEFAULT_PROMPT_LIMIT):
        """给模型看的记忆清单。

        只放生效的；置顶的永远在里面；其余的按「跟这句话的相关度 × 记忆
        新旧 × 最近用过没有 × 重要程度」挑 top N，而不是按插入顺序排队——
        按插入序取前 40 条时，新记的事永远排在末尾，正好被截掉。
        """
        now = datetime.now(LOCAL_TZ)
        query = _grams(text)
        with self._lock:
            # 口味单独摆一块，别混进「你记得的」——那一块是事实，
            # 同一句话出现在两处会让她以为你说了两遍。
            items = [dict(item) for item in self._items
                     if item.get("status") == "active" and item.get("kind") != "style"]
        if not items:
            return "（还没有记下什么。）"

        pinned = [item for item in items if item.get("pinned")]
        rest = [item for item in items if not item.get("pinned")]

        def score(item):
            hit = _overlap(query, _grams(item["text"]))
            fresh = _decay(item.get("created_at"), now)
            # 「最近用过」只该是加分项，不该变成自我强化的回声：
            # 权重压到 0.4，半衰期 3 天，而且只对真的命中这句话的记忆记账。
            used = _decay(item.get("last_used"), now, half_life_days=3) if item.get("last_used") else 0.0
            weight = float(item.get("importance") or 3) / 5.0
            return 4.0 * hit + 1.2 * fresh + 0.4 * used + 0.8 * weight, hit

        rest.sort(key=lambda item: (score(item)[0], item.get("created_at") or ""), reverse=True)
        room = max(0, int(limit) - len(pinned))
        chosen = pinned + rest[:room]

        # 只有真的跟这句话相关的，才更新 last_used；否则每轮都会把
        # 同一批"上一轮排前面的"再摸一遍，越摸越靠前。
        touched = [item["id"] for item in chosen if score(item)[1] > 0 or item.get("pinned")]
        self.touch_many(touched)

        lines = []
        for item in chosen:
            if item.get("source") == "user_explicit":
                mark = "·"
            else:
                mark = "·（我推断的，主人确认过）"
            lines.append(f"- {item['text']} {mark}")
        return "\n".join(lines)


class ConversationLog:
    """对话流水。一行一条 JSON，追加写，重启不丢。"""

    def __init__(self, path, max_age_hours=6):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_age_hours = max_age_hours
        self._lock = threading.Lock()

    def append(self, role, content, session="default", meta=None):
        if not content:
            return
        record = {"ts": _now(), "session": session, "role": role, "content": content}
        if meta:
            record["meta"] = meta
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def recent(self, limit=12, session="default"):
        """取最近的若干轮。超过 max_age_hours 的旧对话不注入上下文——
        昨天半夜的倾诉不该在第二天早上的提问里回响。"""
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        cutoff = datetime.now(LOCAL_TZ) - timedelta(hours=self.max_age_hours)
        messages = []
        for line in reversed(lines):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("session") != session:
                continue
            try:
                stamp = datetime.fromisoformat(record["ts"])
            except (KeyError, ValueError):
                continue
            if stamp < cutoff:
                break
            messages.append({"role": record["role"], "content": record["content"]})
            if len(messages) >= limit:
                break
        messages.reverse()
        return messages

    def stats(self):
        if not self.path.exists():
            return {"turns": 0, "bytes": 0}
        data = self.path.read_bytes()
        return {"turns": data.count(b"\n"), "bytes": len(data)}


EXTRACT_PROMPT = """从下面这段对话里，找出**主人亲口说过的、关于他自己的、长期有效的事实**。

只记这几种：
- 身份：名字、称呼、职业、所在城市
- 稳定的偏好：明确说喜欢或讨厌什么
- 正在做的大事：项目、考试、长期目标
- 重要的人和日子：家人、纪念日

如果主人**抱怨了你的说话方式**（太长、太啰嗦、别催、别追问、别说教、别这么正式），
用 kind="style" 记一条：一句话概括他希望你怎么说，**不要照抄他的原话**。
这是他在调你，比手动改人格更常用。

**不要记**（这几条最容易写错，务必遵守）：
- 从本地助理的回答里推断出来的东西 —— 你不是在观察主人，你只是在听他说话
- 一次性的状态：今天累了、刚才在忙、现在想休息
- 具体这一次做的事：在某个会议室开会、在某个目录里读文件
- 任何自己都不确定的推测
- 寒暄、情绪、他自己没说出来的意图

判据：如果这条信息一个月后还成立，才写下来。否则不要写。

只输出一个 JSON 数组，每项形如 {"text": "…", "kind": "fact|preference|event|style", "explicit": true, "supersedes": []}。
用第三人称陈述。
`explicit` 表示这条是主人**用自己的话直接说出来的**；如果是你从他的话里间接推出来的，填 false。
`supersedes` 只在一个情况下填：这条新事实是在**更新**下面列出的某条旧事实（比如搬家、换工作、改了称呼），
把那条的 id 填进来；否则留空数组。不确定就留空——宁可多一条，也不要错误地废掉旧记忆。
**没有真正值得记的，就输出 []。空数组比垃圾好。** 不要输出任何别的文字。

（本节没有任何示例事实。不要编造示例，也不要输出本段文字。）

__EXISTING__

对话：
__CONVERSATION__
"""


def _normalized(text):
    return " ".join((text or "").split()).strip()


def extract_candidates(call_model, user_text, assistant_text, max_items=3, existing=None):
    """让模型从一轮对话里抽候选记忆。call_model(prompt) -> str。

    每条带上 explicit：主人亲口说的，还是她自己推出来的。两者都只进候选，
    要主人点头才算——这条线不能省。

    existing: 已经生效、且跟这轮对话可能相关的记忆 [{id, text}]。
    只用来判断"这条是不是在更新旧事实"。模型说要替换哪条，也只是候选，
    真正把旧条目标记成 superseded 是在主人点「收下」的时候。

    有一个坑必须堵：提示词里如果写了示例事实，小模型会把它当成"对话内容"
    原样输出，于是每次聊天都会凭空多出两条假记忆。所以这里再做一道过滤，
    把和提示词本身重合的候选直接丢掉。
    """
    conversation = f"主人：{user_text}\n0：{assistant_text}"
    if existing:
        lines = "\n".join(f"- {item['id']}: {item['text']}" for item in existing)
        existing_block = ("## 她已经记住的（只用来判断是否更新，不要复述，不要当对话内容）\n" + lines)
    else:
        existing_block = "（她还没有相关的旧记忆。）"
    prompt = (EXTRACT_PROMPT
              .replace("__EXISTING__", existing_block)
              .replace("__CONVERSATION__", conversation))
    raw = call_model(prompt) or ""
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end < start:
        return []
    try:
        parsed = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return []
    instruction = _normalized(EXTRACT_PROMPT)
    valid_ids = {item["id"] for item in (existing or [])}
    candidates = []
    for item in parsed[:max_items]:
        if isinstance(item, dict) and item.get("text"):
            text = _normalized(str(item["text"]))[:MAX_MEMORY_CHARS]
            kind = item.get("kind") if item.get("kind") in ("fact", "preference", "event", "style") else "fact"
            explicit = bool(item.get("explicit"))
            raw_sup = item.get("supersedes") or []
            if isinstance(raw_sup, str):
                raw_sup = [raw_sup]
            supersedes = [str(s) for s in raw_sup if str(s) in valid_ids][:3]
        elif isinstance(item, str) and item.strip():
            text = _normalized(item)[:MAX_MEMORY_CHARS]
            kind, explicit, supersedes = "fact", False, []
        else:
            continue
        if not text or text in instruction:
            continue
        candidates.append({"text": text, "kind": kind, "explicit": explicit,
                           "supersedes": supersedes})
    return candidates
