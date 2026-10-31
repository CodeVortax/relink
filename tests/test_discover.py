"""Offline tests for generic mode (yt-dlp fallback) and site discovery.
Run:  python -m unittest discover -s tests -v
"""
import json
import os
import stat
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from relink import cli, discover, generic, jobs  # noqa: E402
from relink.net import Client  # noqa: E402

HOME = """<html lang="ar"><head><title>موقع مسلسلات</title>
<link href="/wp-content/themes/cimaclub/style.css"><style>.a{color:red}</style></head><body>
<form action="/" method="get"><input name="s"></form>
<a href="/series/the-mentalist-s03e17/">مسلسل The Mentalist الحلقة 17</a>
<a href="/series/house-s01e01/">مسلسل House الحلقة 1</a>
<a href="/movie/inception/">فيلم Inception</a>
<a href="/about">about</a>
<script>var big = "%s";</script>
<script>player({file: "x.m3u8"});</script></body></html>""" % ("x" * 5000)

WATCH = """<html><body><iframe src="https://mixdrop.co/e/abc"></iframe>
<a href="/dl/abc">تحميل الحلقة</a></body></html>"""

DL = "<html><body><form action='/go' method='post'><input type='hidden' name='id' value='1'>" \
     "<input type='submit'></form></body></html>"

SEARCH = "<html><body><a href='/series/the-mentalist-s03e17/'>The Mentalist</a></body></html>"


def start_site(theme="cimaclub"):
    home = HOME.replace("cimaclub", theme)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            p = self.path
            if p.startswith("/?s="):
                body = SEARCH
            elif p == "/":
                body = home
            elif p.startswith("/series/"):
                body = WATCH
            elif p.startswith("/dl/"):
                body = DL
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class Parsers(unittest.TestCase):
    def test_exclude(self):
        self.assertTrue(discover.excluded("https://en.wikipedia.org/wiki/X"))
        self.assertTrue(discover.excluded("https://www.imdb.com/title/tt1"))
        self.assertFalse(discover.excluded("https://cima-club.tv/"))

    def test_forum_and_quote_pages_do_not_score(self):
        forum = ('<html><title>Forum</title><form action="/s"><input name="q"></form>'
                 '<a href="/show/1">show</a><a href="/show/2">show</a><a href="/stream/3">stream</a>'
                 '<a href="/download-app">download app</a></html>')

        class C:
            def request(self, url, **k):
                from relink.net import Response
                import email.message
                return Response(200, url, email.message.Message(), forum.encode())

        fp = discover.fingerprint(C(), "https://forum.example")
        self.assertLess(fp["score"], 3)

    def test_noise_hosts_are_not_embeds(self):
        html = '<iframe src="https://www.google.com/maps"></iframe><iframe src="https://voe.sx/e/1">'
        self.assertEqual(discover.embed_hosts(html), ["voe.sx"])

    def test_pasted_google_text(self):
        text = ("سيما وبس\nhttps://vid.mycima.cc\nhttps://vid.mycima.cc/tag.php?t=مسلسل-the-mentalis...\n"
                "ايجي ديد\nhttps://tv10.egydead.live\nPrime Video\nhttps://www.primevideo.com\n"
                "https://topcinemaa.vip\nhttps://tv10.egydead.live/")
        urls = discover.urls_from_text(text)
        self.assertEqual(urls, ["https://vid.mycima.cc", "https://vid.mycima.cc/",
                                "https://tv10.egydead.live", "https://topcinemaa.vip",
                                "https://tv10.egydead.live/"])

    def test_expand_title(self):
        qs = discover.expand_title("The Mentalist")
        self.assertEqual(len(qs), 5)
        self.assertTrue(any("مشاهدة" in q for q in qs))

    def test_trim_keeps_media_scripts(self):
        out = discover.trim_html(HOME)
        self.assertNotIn("color:red", out)
        self.assertIn("x.m3u8", out)
        self.assertLess(len(out), len(HOME))


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.s1, self.o1 = start_site("cimaclub")
        self.s2, self.o2 = start_site("cimaclub")      # a clone: same theme
        self.s3, self.o3 = start_site("othertheme")

    def tearDown(self):
        for s in (self.s1, self.s2, self.s3):
            s.shutdown()

    def test_fingerprint(self):
        fp = discover.fingerprint(Client(retries=1, timeout=5), self.o1)
        self.assertTrue(fp["ok"])
        self.assertEqual(fp["family"], "wordpress:cimaclub")
        self.assertEqual(fp["search"]["param"], "s")
        self.assertEqual(fp["watch_links"], 3)
        self.assertGreaterEqual(fp["score"], 5)

    def test_unreachable_site_is_reported_not_fatal(self):
        fp = discover.fingerprint(Client(retries=1, timeout=2, sleep=lambda s: None),
                                  "http://127.0.0.1:9")
        self.assertFalse(fp["ok"])
        self.assertEqual(discover.score(fp), 0)

    def test_pipeline_groups_families_and_bundles(self):
        out = Path(self.tmp) / "d"
        logs = []
        res = discover.run(Client(retries=1, timeout=5), out, sites=[self.o1, self.o2, self.o3],
                           log=logs.append, query="mentalist")
        fams = {k: len(v) for k, v in res["families"].items()}
        self.assertEqual(fams["wordpress:cimaclub"], 2)       # the clones share one family
        self.assertEqual(fams["wordpress:othertheme"], 1)
        with tarfile.open(res["bundle"]) as tar:
            names = tar.getnames()
            self.assertIn("report.md", names)
            key = discover.site_key(self.o1)
            for part in ("info.json", "home.html", "search.html", "watch.html", "download.html"):
                self.assertIn(f"{key}/{part}", names)
            info = json.load(tar.extractfile(f"{key}/info.json"))
            self.assertEqual(info["family"], "wordpress:cimaclub")
        self.assertLess(res["bundle"].stat().st_size, 20000)

    def test_result_page_from_search_is_sampled(self):
        out = Path(self.tmp) / "d"
        res = discover.run(Client(retries=1, timeout=5), out,
                           sites=[self.o1 + "/series/the-mentalist-s03e17/"], log=lambda m: None)
        with tarfile.open(res["bundle"]) as tar:
            self.assertIn(f"{discover.site_key(self.o1)}/result.html", tar.getnames())

    def test_second_run_continues_from_saved_results(self):
        out = Path(self.tmp) / "d"
        discover.run(Client(retries=1, timeout=5), out, sites=[self.o1], log=lambda m: None)
        logs = []
        discover.run(Client(retries=1, timeout=5), out, sites=[self.o1], log=logs.append)
        self.assertFalse([m for m in logs if "studying" in m])


class Generic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["RELINK_HOME"] = self.tmp
        fake = Path(self.tmp) / "fake-ytdlp"
        fake.write_text("#!/bin/sh\necho \"$@\" > \"%s/args.txt\"\nexit ${FAKE_EXIT:-0}\n" % self.tmp)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        os.environ["RELINK_YTDLP"] = str(fake)
        self.out = Path(self.tmp) / "out"

    def tearDown(self):
        os.environ.pop("RELINK_YTDLP", None)
        os.environ.pop("FAKE_EXIT", None)

    def args(self):
        return (Path(self.tmp) / "args.txt").read_text()

    def test_unknown_site_goes_to_ytdlp_and_is_saved_as_job(self):
        code = cli.main(["get", "https://example.org/video/1", "--out", str(self.out)])
        self.assertEqual(code, 0)
        a = self.args()
        self.assertIn("-c", a.split())
        self.assertIn("infinite", a)
        self.assertIn(str(self.out), a)
        j = jobs.load_all()[0]
        self.assertEqual((j["engine"], j["status"]), ("ytdlp", "done"))

    def test_quality_low_and_dry(self):
        cli.main(["get", "https://example.org/v", "--quality", "low", "--dry"])
        a = self.args()
        self.assertIn("-g", a.split())
        self.assertIn("480", a)

    def test_failure_is_kept_and_resume_retries_it(self):
        os.environ["FAKE_EXIT"] = "1"
        self.assertEqual(cli.main(["get", "https://example.org/v", "--out", str(self.out)]), 1)
        self.assertEqual(jobs.load_all()[0]["status"], "failed")
        os.environ["FAKE_EXIT"] = "0"
        self.assertEqual(cli.main(["resume"]), 0)
        self.assertEqual(jobs.load_all()[0]["status"], "done")

    def test_missing_ytdlp_gives_a_clear_error(self):
        os.environ["RELINK_YTDLP"] = ""
        orig = generic.shutil.which
        generic.shutil.which = lambda n: None
        spec = generic.importlib.util.find_spec
        generic.importlib.util.find_spec = lambda n: None
        try:
            self.assertEqual(cli.main(["get", "https://example.org/v"]), 1)
        finally:
            generic.shutil.which = orig
            generic.importlib.util.find_spec = spec


if __name__ == "__main__":
    unittest.main()
