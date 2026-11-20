"""记忆的关系网。

记忆本来是一条条事实（"在准备考研"）。这一层把它们拆成**实体和关系**：

    （主人）—[正在做]→（某项目）—[截止]→（下周三）
    （某宠物）—[是]→（猫，属于主人）

为什么值得做：一条条事实只能被"引用"，关系网能被"推理"。
"你上次跟某人吵架就是因为那个项目"这种话，靠事实列表是说不出来的。

设计上的两个克制：
  · 只用**已确认**的记忆建图。候选还没得到主人点头，不该进她的认知。
  · 图是**派生数据**，随时可以从记忆重建。坏了删掉就行，不丢东西。
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOCAL_TZ = timezone(timedelta(hours=8))

ENTITY_TYPES = ("person", "pet", "place", "org", "project", "thing", "time", "goal")

EXTRACT_PROMPT = """把下面这些**已经确认的事实**拆成实体和关系。

实体：人、宠物、地方、组织、项目、东西、时间、目标。
关系：用简短的中文动词短语，比如 正在做、喜欢、住在、养了、截止、属于、认识。

规则：
- 只输出事实里**明确出现过**的东西，不要推测没提到的。
- "主人/他"就是主人本人，用实体名「主人」表示。
- 没有可拆的就输出空数组。
- 不要编造示例。

只输出一个 JSON 对象：
{"entities": [{"name": "…", "type": "person|pet|place|org|project|thing|time|goal"}],
 "relations": [{"from": "…", "rel": "…", "to": "…"}]}

事实：
__FACTS__
"""


def _now():
    return datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


class GraphStore:
    def __init__(self, path, extractor=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.extractor = extractor
        self._lock = threading.RLock()
        self.data = {"built_at": None, "entities": {}, "relations": []}
        self.load()

    # ---------- 持久化 ----------

    def load(self):
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            except (FileNotFoundError, json.JSONDecodeError):
                return
            if not isinstance(raw, dict):
                return
            entities = raw.get("entities")
            relations = raw.get("relations")
            if isinstance(entities, dict):
                self.data["entities"] = {
                    str(k): v for k, v in entities.items() if isinstance(v, dict)
                }
            if isinstance(relations, list):
                self.data["relations"] = [r for r in relations if isinstance(r, dict)]
            self.data["built_at"] = raw.get("built_at")

    def save(self):
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)

    # ---------- 建图 ----------

    @staticmethod
    def _facts(memories, limit=60):
        lines = []
        for item in memories[:limit]:
            text = (item.get("text") or "").strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)

    def rebuild(self, memories):
        """从已确认的记忆重建整张图。记忆是源，图是派生。"""
        facts = self._facts(memories)
        if not facts or self.extractor is None:
            with self._lock:
                self.data = {"built_at": _now(), "entities": {}, "relations": []}
            self.save()
            return self.data

        raw = self.extractor(EXTRACT_PROMPT.replace("__FACTS__", facts)) or ""
        parsed = self._parse(raw)
        with self._lock:
            self.data = {
                "built_at": _now(),
                "entities": parsed["entities"],
                "relations": parsed["relations"],
            }
        self.save()
        return self.data

    @staticmethod
    def _parse(raw):
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end < start:
            return {"entities": {}, "relations": []}
        try:
            payload = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            return {"entities": {}, "relations": []}

        entities = {}
        for item in payload.get("entities") or []:
            if not isinstance(item, dict):
                continue
            name = " ".join(str(item.get("name") or "").split())[:40]
            if not name:
                continue
            kind = item.get("type") if item.get("type") in ENTITY_TYPES else "thing"
            entities.setdefault(name, {"type": kind, "mentions": 0})
            entities[name]["mentions"] = entities[name].get("mentions", 0) + 1

        seen = set()
        relations = []
        for item in payload.get("relations") or []:
            if not isinstance(item, dict):
                continue
            source = " ".join(str(item.get("from") or "").split())[:40]
            target = " ".join(str(item.get("to") or "").split())[:40]
            label = " ".join(str(item.get("rel") or "").split())[:24]
            if not source or not target or not label:
                continue
            # 两头都得是认过的实体，不然图会飘
            if source not in entities:
                entities[source] = {"type": "thing", "mentions": 1}
            if target not in entities:
                entities[target] = {"type": "thing", "mentions": 1}
            key = (source, label, target)
            if key in seen:
                continue
            seen.add(key)
            relations.append({"from": source, "rel": label, "to": target})

        return {"entities": entities, "relations": relations}

    # ---------- 读 ----------

    def prompt_block(self, limit=18):
        """压缩成几行塞进提示词。按提及次数排序，重要的在里面。"""
        with self._lock:
            entities = dict(self.data.get("entities") or {})
            relations = list(self.data.get("relations") or [])
        if not relations and not entities:
            return ""

        ranked = sorted(entities.items(), key=lambda kv: kv[1].get("mentions", 0), reverse=True)
        lines = []
        used = set()
        for source, label, target in [(r["from"], r["rel"], r["to"]) for r in relations][:limit]:
            used.update({source, target})
            lines.append(f"- {source} {label} {target}")
        leftovers = [name for name, _ in ranked if name not in used][:max(0, limit - len(lines))]
        if leftovers:
            lines.append("- 还提到过：" + "、".join(leftovers))
        return "\n".join(lines)

    def summary(self):
        with self._lock:
            return {
                "built_at": self.data.get("built_at"),
                "entities": len(self.data.get("entities") or {}),
                "relations": len(self.data.get("relations") or []),
            }
