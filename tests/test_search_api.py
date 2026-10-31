"""Offline tests for the search API: parsers per engine (on small sample pages), merging,
error reporting and the local JSON server.  Run:  python -m unittest discover -s tests -v
"""
import base64
import json
import sys
import threading
import unittest
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relink import search_api as sa  # noqa: E402
from relink.net import NetError  # noqa: E402

GOOGLE = """<div><a href="/url?q=https://cima-time.com/series/x&amp;sa=U&amp;ved=1"><h3>The Mentalist S1 - Cima Time</h3></a>
<div>Watch episode 1 online</div></div>
<div><a href="/url?q=https://www.google.com/maps&amp;sa=U"><h3>maps</h3></a></div>
<div><a href="/url?q=https://topcinemaa.vip/&amp;sa=U"><h3>توب سينما</h3></a><div>مسلسل مترجم</div></div>"""

DDG = ('<div class="result"><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg='
       + quote("https://cima-time.com/series/x", safe="") + '&amp;rut=1">Cima Time</a>'
       '<a class="result__snippet" href="x">Episode list &amp; links</a></div>'
       '<a class="other" href="https://nope.com">no</a>')

_REAL = "https://egydead.live/m"
_U = "a1" + base64.urlsafe_b64encode(_REAL.encode()).decode().rstrip("=")
BING = (f'<ol><li class="b_algo"><h2><a href="https://www.bing.com/ck/a?!&amp;p=1&amp;u={_U}&amp;ntb=1">'
        '<strong>Egy</strong>Dead</a></h2><div class="b_caption"><p>Series page</p></div></li>'
        '<li class="b_algo"><h2><a href="https://plain.example/z">Plain</a></h2></li></ol>')

BRAVE = ('<div class="snippet svelte-1" data-type="web"><a href="https://shahidwbas.tv/s" class="l1">'
         '<div class="title">Shahid W Bas</div></a><div class="snippet-description">Series</div></div>')

MOJEEK = ('<ul class="results-standard"><li><a class="ob" href="https://moviz.land/m">x</a>'
          '<h2><a class="title" href="https://moviz.land/m">MovizLand</a></h2>'
          '<p class="s">Season 1</p></li></ul>')

HEADING_ONLY = '<a href="https://some.site/p"><h3>A heading result</h3></a><p>text</p>'


class FakeClient:
    """Answers by engine host. A value that is an Exception is raised."""
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def request(self, url, data=None, referer=None, ajax=False):
        self.calls.append((url, data))
        for host, body in self.pages.items():
            if host in url:
                if isinstance(body, Exception):
                    raise body

                class R:
                    status, text = 200, body
                return R
        raise NetError("no route " + url)


class Parsers(unittest.TestCase):
    def test_google(self):
        r = sa.ENGINES["google"].parse(GOOGLE)
        self.assertEqual([x["url"] for x in r], ["https://cima-time.com/series/x", "https://topcinemaa.vip/"])
        self.assertEqual(r[0]["title"], "The Mentalist S1 - Cima Time")
        self.assertIn("Watch episode", r[0]["snippet"])
        self.assertEqual(r[1]["title"], "توب سينما")

    def test_ddg(self):
        r = sa.ENGINES["ddg"].parse(DDG)
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["url"], "https://cima-time.com/series/x")
        self.assertEqual(r[0]["snippet"], "Episode list & links")

    def test_bing_unwraps_redirect(self):
        r = sa.ENGINES["bing"].parse(BING)
        self.assertEqual([x["url"] for x in r], [_REAL, "https://plain.example/z"])
        self.assertEqual(r[0]["title"], "EgyDead")
        self.assertEqual(r[0]["snippet"], "Series page")

    def test_brave_and_mojeek(self):
        b = sa.ENGINES["brave"].parse(BRAVE)
        self.assertEqual((b[0]["url"], b[0]["title"]), ("https://shahidwbas.tv/s", "Shahid W Bas"))
        m = sa.ENGINES["mojeek"].parse(MOJEEK)
        self.assertEqual((m[0]["url"], m[0]["title"], m[0]["snippet"]),
                         ("https://moviz.land/m", "MovizLand", "Season 1"))

    def test_generic_fallback(self):
        r = sa.generic_parse(HEADING_ONLY, own_hosts=("bing.",))
        self.assertEqual(r[0]["url"], "https://some.site/p")
        self.assertEqual(r[0]["title"], "A heading result")

    def test_clean_url(self):
        self.assertIsNone(sa.clean_url("/relative"))
        self.assertEqual(sa.clean_url("/url?q=https%3A%2F%2Fa.tv%2Fx%3Fp%3D1&sa=U"), "https://a.tv/x?p=1")

    def test_requests_carry_lang_region_and_page(self):
        url, _ = sa.ENGINES["google"].request("q", 1, "ar", "eg")
        self.assertIn("hl=ar", url)
        self.assertIn("gl=eg", url)
        self.assertIn("start=10", url)
        self.assertIn("first=11", sa.ENGINES["bing"].request("q", 1, None, None)[0])
        _, data = sa.ENGINES["ddg"].request("مسلسل", 0, "ar", "xa")
        self.assertIn("kl=xa-ar", data)


class Search(unittest.TestCase):
    def test_merge_ranks_pages_found_by_several_engines_first(self):
        c = FakeClient({"google.com": GOOGLE, "duckduckgo.com": DDG, "bing.com": BING})
        out = sa.search("mentalist", ["google", "ddg", "bing"], client=c, pause=0)
        urls = [r["url"] for r in out["results"]]
        self.assertEqual(urls[0], "https://cima-time.com/series/x")
        self.assertEqual(sorted(out["results"][0]["engines"]), ["ddg", "google"])
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual([r["rank"] for r in out["results"]], list(range(1, len(urls) + 1)))
        self.assertTrue(all(v["ok"] for v in out["engines"].values()))

    def test_one_failing_engine_does_not_stop_the_others(self):
        c = FakeClient({"google.com": NetError("timeout"), "duckduckgo.com": DDG,
                        "bing.com": "<html>Our systems have detected unusual traffic, captcha</html>"})
        out = sa.search("x", ["google", "ddg", "bing"], client=c, pause=0)
        self.assertEqual(len(out["results"]), 1)
        self.assertFalse(out["engines"]["google"]["ok"])
        self.assertIn("timeout", out["engines"]["google"]["error"])
        self.assertIn("blocked", out["engines"]["bing"]["error"])
        self.assertTrue(out["engines"]["ddg"]["ok"])

    def test_pages_and_limit(self):
        c = FakeClient({"bing.com": BING})
        out = sa.search("x", "bing", pages=2, limit=1, client=c, pause=0)
        self.assertEqual(len(c.calls), 2)
        self.assertEqual(len(out["results"]), 1)

    def test_unknown_engine(self):
        with self.assertRaises(ValueError):
            sa.search("x", ["altavista"], client=FakeClient({}))


class Server(unittest.TestCase):
    def test_json_api(self):
        seen = {}

        def fake(q, engines, pages, lang, region, limit):
            seen.update(q=q, engines=engines, pages=pages, lang=lang, limit=limit)
            return {"query": q, "results": [{"title": "t", "url": "https://a.tv"}], "engines": {}}

        srv = sa.make_server(0, search_fn=fake)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_address[1]}"
            body = json.load(urlopen(base + "/search?q=" + quote("مسلسل") + "&engines=google,ddg&lang=ar&limit=5"))
            self.assertEqual(body["results"][0]["url"], "https://a.tv")
            self.assertEqual((seen["q"], seen["engines"], seen["lang"], seen["limit"]),
                             ("مسلسل", "google,ddg", "ar", 5))
            self.assertIn("usage", json.load(urlopen(base + "/")))
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()
