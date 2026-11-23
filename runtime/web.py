"""联网。只用标准库，不需要任何 API key。

挑了三个在这台机器上实测能通的源：
  · open-meteo  天气（自带中文地理编码，WMO 代码 → 中文，稳定）
  · 60s-api     每天 60 秒读懂世界（当日要闻，正好是"大概发生了什么"）
  · 搜狗 / DuckDuckGo  搜索（搜狗中文相关性最好，DDG 兜底）

原则：
  · 联网失败就明说失败，绝不编一个看起来合理的答案。
  · 所有结果带缓存，同一分钟内不会重复打别人的服务器。
  · 网页正文是不可信数据，只当资料，不当指令。
"""

import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
# 只给 UA 是不够的：搜狗和 DuckDuckGo 会返回几 KB 的拦截页。
# 带上完整的浏览器请求头之后，360 和百度才会正常回结果。
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
    "Upgrade-Insecure-Requests": "1",
}
MAX_BYTES = 3 * 1024 * 1024


class WebError(RuntimeError):
    """联网失败。调用方负责如实告诉主人。"""


# ---------- 底座 ----------

def _fetch(url, timeout=12, headers=None):
    merged = dict(BASE_HEADERS)
    merged.update(headers or {})
    request = Request(url, headers=merged)
    # 弱网下 open-meteo 偶尔 SSL 握手超时，一次失败就整条简报没有天气。
    # 只对"连接层"的问题重试一次；对方明确回了 4xx/5xx 就别再打人家。
    raw = None
    last_error = None
    for attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_BYTES)
            break
        except HTTPError as exc:
            raise WebError(f"对方返回 {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.8)
                continue
    if raw is None:
        reason = getattr(last_error, "reason", last_error)
        if isinstance(last_error, TimeoutError) or "timed out" in str(reason):
            raise WebError("等太久了，超时") from None
        raise WebError(f"连不上（{reason}）") from None
    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _fetch_json(url, timeout=12):
    text = _fetch(url, timeout=timeout)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise WebError("对方返回的不是 JSON") from None


_cache = {}


def _cached(key, ttl, producer):
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    value = producer()
    _cache[key] = (time.time() + ttl, value)
    return value


def clear_cache():
    _cache.clear()


# ---------- 天气 ----------

# open-meteo 用的是 WMO 天气代码，翻成中文比较稳。
WMO = {
    0: "晴", 1: "大致晴", 2: "局部多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "毛毛雨", 53: "小雨", 55: "中雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷雨伴冰雹",
}


def geocode(city):
    """城市名 → 经纬度。支持中文。"""
    url = ("https://geocoding-api.open-meteo.com/v1/search?name="
           + _quote(city) + "&count=1&language=zh&format=json")
    data = _fetch_json(url, timeout=10)
    results = data.get("results") or []
    if not results:
        raise WebError(f"找不到这个地名：{city}")
    top = results[0]
    return {
        "name": top.get("name") or city,
        "admin": top.get("admin1") or "",
        "country": top.get("country") or "",
        "lat": top["latitude"],
        "lon": top["longitude"],
    }


def weather(city, days=1):
    """今天的天气（可选带明天）。返回结构化字段。"""
    days = max(1, min(int(days or 1), 3))

    def produce():
        place = geocode(city)
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={place['lat']}&longitude={place['lon']}"
               "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
               "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
               f"&timezone=auto&forecast_days={days}")
        data = _fetch_json(url, timeout=12)
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        code = int(current.get("weather_code") or 0)
        today_code = int((daily.get("weather_code") or [code])[0] or code)
        days_out = []
        for index in range(len(daily.get("time") or [])):
            days_out.append({
                "date": (daily.get("time") or [])[index],
                "description": WMO.get(int((daily.get("weather_code") or [0] * 9)[index] or 0), "天气不明"),
                "high": _first(daily.get("temperature_2m_max") or [], index),
                "low": _first(daily.get("temperature_2m_min") or [], index),
                "rain_chance": _first(daily.get("precipitation_probability_max") or [], index),
            })
        return {
            "city": place["name"],
            "description": WMO.get(code) or WMO.get(today_code) or "天气不明",
            "temperature": current.get("temperature_2m"),
            "feels_like": current.get("apparent_temperature"),
            "high": _first(daily.get("temperature_2m_max")),
            "low": _first(daily.get("temperature_2m_min")),
            "rain_chance": _first(daily.get("precipitation_probability_max")),
            "wind": current.get("wind_speed_10m"),
            "humidity": current.get("relative_humidity_2m"),
            "days": days_out,
        }

    return _cached(("weather", city, days), 1200, produce)


def weather_text(city, days=1):
    data = weather(city, days=days)
    parts = [f"{data['city']}现在 {data['description']}，{_num(data['temperature'])}°C"]
    if data["feels_like"] is not None and abs((data["feels_like"] or 0) - (data["temperature"] or 0)) >= 2:
        parts.append(f"体感 {_num(data['feels_like'])}°C")
    if data["high"] is not None and data["low"] is not None:
        parts.append(f"今天 {_num(data['low'])}~{_num(data['high'])}°C")
    if data["rain_chance"] is not None:
        parts.append(f"降水概率 {_num(data['rain_chance'])}%")
    if data["wind"] is not None:
        parts.append(f"风速 {_num(data['wind'])} km/h")
    text = "，".join(parts)
    for index, day in enumerate((data.get("days") or [])[:max(1, int(days or 1))], start=0):
        if index == 0:
            continue
        label = "明天" if index == 1 else f"{(day['date'] or '')[5:]}"
        text += (f"。{label} {day['description']}，"
                 f"{_num(day['low'])}~{_num(day['high'])}°C，"
                 f"降水概率 {_num(day['rain_chance'])}%")
    return text


# ---------- 今天发生了什么 ----------

def headlines(limit=8):
    """当日要闻。60s-api 一天更新一次，正好够用。"""
    def produce():
        data = _fetch_json("https://60s-api.viki.moe/v2/60s", timeout=12)
        payload = data.get("data") or {}
        news = payload.get("news") or []
        if not news:
            raise WebError("今天没有拿到要闻")
        return {"date": payload.get("date"), "news": [str(n).strip() for n in news if str(n).strip()]}

    data = _cached("headlines", 1800, produce)
    return data["news"][:limit]


# ---------- 搜索 ----------

def search(query, limit=5):
    """搜索。360 优先，百度兜底。只返回标题 / 摘要 / 链接。

    实测（2026-11）：搜狗和 DuckDuckGo 加了反爬，只回几 KB 的拦截页；
    必应会返回**别的查询**的缓存结果，不可信。360 和百度是稳的。
    代价是这两家的链接是加密跳转，点得开但不好看——所以只在需要对来源时给链接。
    """
    query = (query or "").strip()
    if not query:
        raise WebError("搜索需要关键词")

    def produce():
        errors = []
        for name, engine in (("360", _so360), ("百度", _baidu)):
            try:
                results = engine(query, limit)
                if results:
                    return results
                errors.append(f"{name}：没有结果")
            except WebError as exc:
                errors.append(f"{name}：{exc}")
        raise WebError("；".join(errors))

    return _cached(("search", query.lower(), limit), 600, produce)


def _so360(query, limit):
    html = _fetch("https://www.so.com/s?q=" + _quote(query), timeout=12)
    titles = [(m.group(1), _strip_tags(m.group(2))) for m in
              re.finditer(r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)]
    snippets = [_strip_tags(m.group(1)) for m in
                re.finditer(r'<p[^>]*class="[^"]*res-desc[^"]*"[^>]*>(.*?)</p>', html, re.S)]
    return _pack(titles, snippets, limit)


def _baidu(query, limit):
    html = _fetch("https://www.baidu.com/s?wd=" + _quote(query), timeout=12)
    titles = [(m.group(1), _strip_tags(m.group(2))) for m in
              re.finditer(r'<h3[^>]*class="[^"]*\bt\b[^"]*"[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)]
    return _pack(titles, [], limit)


def _pack(titles, snippets, limit):
    results = []
    for index, (raw_url, title) in enumerate(titles):
        # 太短的"标题"多半是导航栏或图标文字，不是结果
        if not title or len(title) < 3:
            continue
        snippet = snippets[index] if index < len(snippets) else ""
        if snippet.startswith(title):
            snippet = snippet[len(title):].strip()
        results.append({"title": title, "url": _clean_url(raw_url), "snippet": snippet[:160]})
        if len(results) >= limit:
            break
    return results


def _clean_url(url):
    from urllib.parse import unquote
    url = (url or "").replace("&amp;", "&").strip()
    # 各家跳转链接的参数名不一样：DuckDuckGo 用 uddg，其余常见的是 url
    for key in ("uddg", "url", "u"):
        match = re.search(r"[?&]" + key + r"=([^&]+)", url)
        if match:
            url = unquote(match.group(1))
            break
    if url.startswith("//"):
        url = "https:" + url
    return url


def _strip_tags(html):
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return " ".join(text.split())


# ---------- 给提示词用的简报 ----------

def brief(city, news_count=3):
    """几行「现在大概是什么情况」。任何一项失败都不影响其他项。"""
    lines = []
    try:
        lines.append(weather_text(city))
    except WebError as exc:
        lines.append(f"天气没拿到（{exc}）")
    try:
        items = headlines(limit=news_count)
        if items:
            lines.append("今天的事：" + "；".join(items))
    except WebError as exc:
        lines.append(f"要闻没拿到（{exc}）")
    return "\n".join(lines)


# ---------- 小工具 ----------

def _quote(text):
    from urllib.parse import quote
    return quote(text, safe="")


def _first(value, index=0):
    if isinstance(value, (list, tuple)):
        return value[index] if len(value) > index else None
    return value if index == 0 else None


def _num(value):
    try:
        return f"{float(value):.0f}"
    except (TypeError, ValueError):
        return "—"
