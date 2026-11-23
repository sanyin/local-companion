"""怎么从一堆记忆里挑出这轮该看的那十几条。

这一层是纯函数：给一组条目、一句查询、一个"现在"，返回该注入的条目和
每条的分量。不碰数据库、不碰模型、不碰时钟——所以能被单测钉死。

为什么从 n-gram 升级到混合打分：
    字符 2-gram 只认字面。「我搬到北京了」和「主人住在上海」一个 2-gram
    都不重合，可它们说的是同一件事。所以字面分（lex）之外还要语义分（sem）。
    语义分需要向量，向量可能没有（模型没装），所以 sem 缺失时整个公式
    必须还能跑——降级不是异常，是正常路径。

三种分量的分工：
    lex    精确命中，短查询靠它（"上海"、"考研"）
    sem    改写命中，长句和同义靠它
    fresh  新近度。两周前的偏好还该记得，两个月前的一次性事件不该抢位置
    use    最近用过。权重压得很低，因为它会自我强化
    imp    重要程度。身份/偏好比一次性事件更该长期占位置
"""

import math
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))

# 默认权重。调参时只改这里，别在调用点写死数字。
WEIGHTS = {
    "sem": 0.45,
    "lex": 0.25,
    "fresh": 0.15,
    "use": 0.05,
    "imp": 0.10,
}

# 进提示词的默认预算。条数和字数哪个先到就停。
DEFAULT_LIMIT = 12
DEFAULT_CHARS = 700

# 低于这个总分不进池。避免"总分很低也硬塞"。
DEFAULT_TAU = 0.30

# MMR 的去冗余强度。1.0 = 完全不管重复，0 = 只挑最新奇的。
DEFAULT_MMR = 0.7

FRESH_HALF_LIFE_DAYS = 14
USE_HALF_LIFE_DAYS = 3


def now_local():
    return datetime.now(LOCAL_TZ)


def grams(text):
    """字符 2-gram。中文不用分词也能做相关性，代价是偶尔把「上海」「海上」算重。"""
    body = "".join(ch for ch in (text or "").lower() if not ch.isspace())
    if len(body) < 2:
        return {body} if body else set()
    return {body[i:i + 2] for i in range(len(body) - 1)}


def overlap(query_grams, text_grams):
    """查询命中率，按记忆长度开方归一化（长句子不该天然占便宜），封顶 1。"""
    if not query_grams or not text_grams:
        return 0.0
    return min(1.0, len(query_grams & text_grams) / math.sqrt(len(text_grams)))


def decay(stamp, now=None, half_life_days=FRESH_HALF_LIFE_DAYS):
    if not stamp:
        return 0.0
    moment = _parse(stamp)
    if moment is None:
        return 0.0
    age_days = max(0.0, ((now or now_local()) - moment).total_seconds() / 86400)
    return 0.5 ** (age_days / half_life_days)


def cosine(a, b):
    """两个向量的余弦。任一为空就返回 0——没有向量不是错误。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _parse(stamp):
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=LOCAL_TZ)
    return moment


def _heat_factor(row):
    """冷热因子：heat 1.0 时不加分不减分，降到 0 最多打 6 折。

    只降温不删除——所以它是个"往后排"的力度，不是"看不见"的开关。
    """
    heat = row.get("heat")
    if heat is None:
        return 1.0
    return 0.6 + 0.4 * max(0.0, min(1.5, float(heat)))


def score_row(row, query_grams, now=None, query_vec=None, weights=None):
    """算一条记忆的分，并把分量都留下来——没有分量就没法解释、没法调参。"""
    w = dict(WEIGHTS)
    if weights:
        w.update(weights)
    now = now or now_local()

    sem = 0.0
    if query_vec and row.get("vec"):
        sem = cosine(query_vec, row["vec"])

    lex = overlap(query_grams, row.get("_grams") or grams(row.get("text") or ""))
    fresh = decay(row.get("created_at"), now)
    used = decay(row.get("last_used"), now, USE_HALF_LIFE_DAYS) if row.get("last_used") else 0.0
    imp = max(0.0, min(1.0, (float(row.get("importance") or 3) - 1) / 4.0))

    raw = w["sem"] * sem + w["lex"] * lex + w["fresh"] * fresh + w["use"] * used + w["imp"] * imp
    total = raw * _heat_factor(row)

    return {
        "score": total,
        "sem": sem,
        "lex": lex,
        "fresh": fresh,
        "use": used,
        "imp": imp,
        "why": _why(row, sem, lex, fresh, used, imp),
    }


def _why(row, sem, lex, fresh, used, imp):
    """一句话说清它凭什么进来。界面上要显示这句，不然排序没法解释。"""
    if row.get("pinned"):
        return "置顶"
    if lex >= 0.5:
        return "字面命中"
    if sem >= 0.6:
        return "语义命中"
    if fresh >= 0.6 and imp >= 0.6:
        return "近期且重要"
    if fresh >= 0.6:
        return "近期"
    if imp >= 0.75:
        return "重要"
    if used >= 0.5:
        return "最近用过"
    return "填充"


def select(rows, text="", now=None, limit=DEFAULT_LIMIT, chars=DEFAULT_CHARS,
           tau=DEFAULT_TAU, mmr_lambda=DEFAULT_MMR, query_vec=None,
           weights=None, with_trace=False):
    """挑这一轮该进提示词的那些。

    返回 (picked, trace)。picked 是按入选顺序排的 dict 列表，每项多一个
    `_rank` 字段；trace 是每条候选的分量和理由（含没入选的，便于调参）。

    两条规则固定，不参与排序：
      · 置顶的永远在里面；
      · 字符预算超了就停，不为了凑条数硬塞。
    """
    now = now or now_local()
    rows = [dict(item) for item in rows if (item.get("text") or "").strip()]
    query_grams = grams(text)

    picked = [row for row in rows if row.get("pinned")]
    rest = [row for row in rows if not row.get("pinned")]

    scored = []
    for row in rest:
        detail = score_row(row, query_grams, now=now, query_vec=query_vec, weights=weights)
        detail["row"] = row
        scored.append(detail)

    scored.sort(key=lambda item: (item["score"], item["row"].get("created_at") or ""), reverse=True)

    # MMR：从第 2 条起，跟已选集合太像的要扣分，否则 12 条说的是同一件事。
    selected = []
    pool = [detail for detail in scored if _qualifies(detail, tau)]
    # MMR 是平方复杂度。候选上百条时先按分数砍到 60，再去做去冗余——
    # 省下来的时间不影响结果，因为去冗余只在前几名之间才有意义。
    cap = max(int(limit) * 5, 60)
    if len(pool) > cap:
        pool = pool[:cap]
    while pool and (len(picked) + len(selected)) < limit:
        best, best_value = None, -1.0
        for detail in pool:
            value = detail["score"]
            if mmr_lambda < 1.0 and selected:
                value -= (1.0 - mmr_lambda) * _redundancy(detail["row"], selected)
            if value > best_value:
                best, best_value = detail, value
        if best is None:
            break
        pool.remove(best)
        best["_mmr"] = round(best_value, 4)
        selected.append(best)

    if not picked and not selected:
        # 一条都不相关：兜底三条最近的，否则「你记得什么」永远空着。
        # 兜底也不许超过三条——再多就是噪声。
        for detail in scored[:3]:
            detail["_fallback"] = True
            selected.append(detail)

    result = []
    used_chars = 0
    for row in picked:
        row["_rank"] = "pinned"
        row["_score"] = None
        result.append(row)
        used_chars += len(row.get("text") or "")

    for detail in selected:
        row = detail["row"]
        text_len = len(row.get("text") or "")
        if used_chars + text_len > chars and result:
            continue
        row["_rank"] = detail["why"] if not detail.get("_fallback") else "兜底"
        row["_score"] = round(detail["score"], 4)
        row["_parts"] = {k: round(detail[k], 3) for k in ("sem", "lex", "fresh", "use", "imp")}
        result.append(row)
        used_chars += text_len

    trace = [
        {
            "id": detail["row"].get("id"),
            "text": (detail["row"].get("text") or "")[:60],
            "score": round(detail["score"], 4),
            "sem": round(detail["sem"], 3),
            "lex": round(detail["lex"], 3),
            "fresh": round(detail["fresh"], 3),
            "use": round(detail["use"], 3),
            "imp": round(detail["imp"], 3),
            "why": detail["why"],
            "picked": any(detail["row"].get("id") == item.get("id") for item in result),
            "mmr": detail.get("_mmr"),
        }
        for detail in scored
    ]
    return result, trace


def _qualifies(detail, tau):
    """够不够格进候选池。

    只比总分不行：一条刚记下的无关记忆，光靠"新鲜 + 重要"就能到 0.23，
    低于这个数等于没过滤。所以要么总分够，要么字面/语义确实命中。
    """
    if detail["score"] >= tau:
        return True
    return detail["lex"] >= 0.2 or detail["sem"] >= 0.35


def _redundancy(row, selected):
    """这条跟已选的有多像。有向量用向量，没有就退回字面。"""
    worst = 0.0
    row_grams = row.get("_grams") or grams(row.get("text") or "")
    for detail in selected:
        other = detail["row"]
        sim = 0.0
        if row.get("vec") and other.get("vec"):
            sim = cosine(row["vec"], other["vec"])
        if sim <= 0:
            sim = jaccard(row_grams, other.get("_grams") or grams(other.get("text") or ""))
        worst = max(worst, sim)
    return worst
