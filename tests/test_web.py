import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))
import web


class TextHelperTests(unittest.TestCase):
    def test_strip_tags_removes_script_and_tags(self):
        html = "<div><script>var x='<b>';</script><b>你好</b>&nbsp;世界</div>"
        self.assertEqual(web._strip_tags(html), "你好 世界")

    def test_strip_tags_unescapes_entities(self):
        self.assertIn("a&b", web._strip_tags("<p>a&amp;b</p>"))

    def test_clean_url_decodes_duckduckgo_redirect(self):
        url = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1"
        self.assertEqual(web._clean_url(url), "https://example.com/a?b=1")

    def test_clean_url_handles_plain_url(self):
        self.assertEqual(web._clean_url("https://a.b/c"), "https://a.b/c")

    def test_clean_url_unescapes_amp(self):
        self.assertEqual(web._clean_url("https://a.b/c?x=1&amp;y=2"), "https://a.b/c?x=1&y=2")


class PackTests(unittest.TestCase):
    def test_pairs_titles_with_snippets(self):
        titles = [("https://a", "标题一"), ("https://b", "标题二号")]
        snippets = ["摘要一内容够长", "摘要二内容够长"]
        got = web._pack(titles, snippets, limit=5)
        self.assertEqual(got[0]["title"], "标题一")
        self.assertEqual(got[0]["snippet"], "摘要一内容够长")
        self.assertEqual(got[1]["url"], "https://b")

    def test_drops_too_short_titles(self):
        got = web._pack([("https://a", "啊")], [], limit=5)
        self.assertEqual(got, [])

    def test_missing_snippets_are_empty_not_broken(self):
        got = web._pack([("https://a", "标题一")], [], limit=5)
        self.assertEqual(got[0]["snippet"], "")

    def test_respects_limit(self):
        titles = [(f"https://{i}", f"标题{i}号") for i in range(10)]
        self.assertEqual(len(web._pack(titles, [], limit=3)), 3)


class WeatherTests(unittest.TestCase):
    def setUp(self):
        web.clear_cache()
        self.original = web._fetch_json

    def tearDown(self):
        web._fetch_json = self.original
        web.clear_cache()

    def _fake(self, url, timeout=12):
        if "geocoding" in url:
            return {"results": [{"name": "上海", "admin1": "上海市", "country": "中国",
                                 "latitude": 31.2, "longitude": 121.5}]}
        return {
            "current": {"temperature_2m": 14.4, "apparent_temperature": 12.9,
                        "relative_humidity_2m": 81, "weather_code": 3, "wind_speed_10m": 5.2},
            "daily": {"weather_code": [61], "temperature_2m_max": [19.2],
                      "temperature_2m_min": [13.6], "precipitation_probability_max": [70]},
        }

    def test_parses_current_conditions_in_chinese(self):
        web._fetch_json = self._fake
        data = web.weather("上海")
        self.assertEqual(data["city"], "上海")
        self.assertEqual(data["description"], "阴")
        self.assertEqual(round(data["temperature"]), 14)
        self.assertEqual(round(data["high"]), 19)

    def test_weather_text_is_one_readable_line(self):
        web._fetch_json = self._fake
        text = web.weather_text("上海")
        self.assertIn("上海", text)
        self.assertIn("阴", text)
        self.assertIn("降水概率", text)
        self.assertNotIn("\n", text)

    def test_unknown_city_raises(self):
        web._fetch_json = lambda url, timeout=12: {"results": []}
        with self.assertRaises(web.WebError):
            web.weather("不存在的地方")

    def test_second_call_hits_cache(self):
        calls = {"n": 0}

        def counting(url, timeout=12):
            calls["n"] += 1
            return self._fake(url)

        web._fetch_json = counting
        web.weather("上海")
        before = calls["n"]
        web.weather("上海")
        self.assertEqual(calls["n"], before, "第二次应该走缓存，不该再打网络")


class HeadlineTests(unittest.TestCase):
    def setUp(self):
        web.clear_cache()
        self.original = web._fetch_json

    def tearDown(self):
        web._fetch_json = self.original
        web.clear_cache()

    def test_extracts_news_list(self):
        web._fetch_json = lambda url, timeout=12: {
            "code": 200, "data": {"date": "2026-11-15", "news": ["甲", "乙", "丙", "丁"]}}
        self.assertEqual(web.headlines(limit=2), ["甲", "乙"])

    def test_empty_news_is_an_error_not_a_lie(self):
        web._fetch_json = lambda url, timeout=12: {"data": {"news": []}}
        with self.assertRaises(web.WebError):
            web.headlines()


class SearchTests(unittest.TestCase):
    def setUp(self):
        web.clear_cache()
        self.original = web._fetch

    def tearDown(self):
        web._fetch = self.original
        web.clear_cache()

    def test_empty_query_rejected(self):
        with self.assertRaises(web.WebError):
            web.search("   ")

    def test_falls_back_when_first_engine_fails(self):
        def fake_fetch(url, timeout=12, headers=None):
            if "so.com" in url:
                raise web.WebError("被拦了")
            return '<h3 class="t"><a href="http://x/1">百度给的结果标题</a></h3>'

        web._fetch = fake_fetch
        results = web.search("随便查点东西")
        self.assertEqual(len(results), 1)
        self.assertIn("百度给的", results[0]["title"])

    def test_all_engines_failing_says_so(self):
        def boom(url, timeout=12, headers=None):
            raise web.WebError("都没通")

        web._fetch = boom
        with self.assertRaises(web.WebError) as ctx:
            web.search("查不到的词")
        self.assertIn("360", str(ctx.exception))
        self.assertIn("百度", str(ctx.exception))

    def test_parses_360_markup(self):
        html = ('<h3 class="res-title"><a href="https://so.com/link?m=abc">上海天气预报</a></h3>'
                '<p class="res-desc">明天有雨，记得带伞。</p>')
        web._fetch = lambda url, timeout=12, headers=None: html
        results = web.search("上海天气")
        self.assertEqual(results[0]["title"], "上海天气预报")
        self.assertIn("带伞", results[0]["snippet"])


class BriefTests(unittest.TestCase):
    def setUp(self):
        web.clear_cache()
        self.original = (web._fetch_json,)

    def tearDown(self):
        web.clear_cache()

    def test_brief_survives_partial_failure(self):
        # 天气挂了、要闻还在：不能整条简报一起没
        def fake(url, timeout=12):
            if "geocoding" in url:
                raise web.WebError("地理编码挂了")
            return {"data": {"news": ["今天发生了一件事"]}}

        web._fetch_json = fake
        try:
            text = web.brief("上海")
        finally:
            web._fetch_json = self.original[0]
        self.assertIn("天气没拿到", text)
        self.assertIn("今天发生了一件事", text)



class TwoDayWeatherTests(unittest.TestCase):
    """问明天就得有明天的数据——不能拿今天的去推。"""

    def setUp(self):
        web.clear_cache()
        self.original = web._fetch_json

    def tearDown(self):
        web._fetch_json = self.original
        web.clear_cache()

    def _fake(self, url, timeout=12):
        if "geocoding" in url:
            return {"results": [{"name": "上海", "latitude": 31.2, "longitude": 121.5}]}
        self.requested_days = url
        return {
            "current": {"temperature_2m": 9.0, "apparent_temperature": 6.0,
                        "relative_humidity_2m": 50, "weather_code": 0, "wind_speed_10m": 5},
            "daily": {
                "time": ["2026-11-20", "2026-11-21"],
                "weather_code": [0, 61],
                "temperature_2m_max": [12.0, 13.0],
                "temperature_2m_min": [4.0, 5.0],
                "precipitation_probability_max": [0, 80],
            },
        }

    def test_asks_for_two_days(self):
        web._fetch_json = self._fake
        web.weather("上海", days=2)
        self.assertIn("forecast_days=2", self.requested_days)

    def test_text_includes_tomorrow(self):
        web._fetch_json = self._fake
        text = web.weather_text("上海", days=2)
        self.assertIn("明天", text)
        self.assertIn("80", text)          # 明天的降水概率

    def test_single_day_has_no_tomorrow(self):
        web._fetch_json = self._fake
        self.assertNotIn("明天", web.weather_text("上海", days=1))

    def test_days_are_clamped(self):
        web._fetch_json = self._fake
        web.weather("上海", days=99)
        self.assertIn("forecast_days=3", self.requested_days)


if __name__ == "__main__":
    unittest.main()
