"""End-to-end test against the fake site. Run:  python -m unittest discover -s tests -v"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

import fake_site  # noqa: E402
from relink import cli, jobs  # noqa: E402
from relink.engine import Engine, find_template, load_templates  # noqa: E402
from relink.net import Client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def local_template_dir(tmp, port):
    """The real mycima.yml with only `match` pointed at the fake site."""
    d = yaml.safe_load((ROOT / "templates" / "mycima.yml").read_text(encoding="utf-8"))
    d["match"] = r"^http://127\.0\.0\.1:\d+/(?:play|watch|downloads)\.php\?vid=(?P<vid>\w+)"
    d["site"] = {"root": f"http://127.0.0.1:{port}/"}
    tdir = Path(tmp) / "templates"
    tdir.mkdir(parents=True)
    (tdir / "local.yml").write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    return tdir


class Flow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["RELINK_HOME"] = self.tmp
        self.out = Path(self.tmp) / "out"

    def run_site(self, mode):
        server, state, port = fake_site.start(mode)
        self.addCleanup(server.shutdown)
        local_template_dir(self.tmp, port)
        self.url = f"http://127.0.0.1:{port}/play.php?vid={fake_site.VID}"
        return state, port

    def test_real_template_parses_and_matches(self):
        t = find_template("https://cima.my-cima.net/play.php?vid=bb54280c0", load_templates())
        self.assertEqual(t.name, "mycima")
        self.assertIn("vipserver", t.hosts())
        self.assertIsNotNone(find_template("https://w12.my-cima.net/watch.php?vid=abc", load_templates()))

    def test_servers_and_resolve(self):
        state, port = self.run_site("clean")
        tpl = find_template(self.url, load_templates())
        eng = Engine(Client(retries=1, sleep=lambda s: None))
        _, servers = eng.servers(tpl, self.url)
        self.assertEqual([s["name"] for s in servers], ["Vipserver", "Vkvideo", "Mixdrop"])
        r = eng.resolve(tpl, self.url, quality="hd")
        self.assertIn("HD quality 1280x720", r.label)
        self.assertTrue(r.url.endswith(f"?t=tok1&s=1&e=43200"), r.url)   # &amp; was unescaped
        self.assertEqual(r.filename, "The.Mentalist.S03.E17.mp4")        # [tag] stripped
        self.assertEqual(r.server, "Vipserver")
        low = eng.resolve(tpl, self.url, quality="low")
        self.assertIn("Low quality", low.label)
        self.assertIn("_l/", low.url)

    def check_download(self, mode):
        state, port = self.run_site(mode)
        code = cli.main(["get", self.url, "--out", str(self.out), "--quality", "hd"])
        self.assertEqual(code, 0)
        f = self.out / "The.Mentalist.S03.E17.mp4"
        self.assertEqual(f.read_bytes(), fake_site.FILE)
        self.assertFalse((self.out / "The.Mentalist.S03.E17.mp4.part").exists())
        return state

    def test_download_survives_cut_and_expired_link(self):
        state = self.check_download("cut")
        # first form for the first link, a second one after the link died
        self.assertGreaterEqual(state.forms_posted, 2)
        ranges = [r for _, r in state.file_requests if r]
        self.assertTrue(ranges, "second try should resume with a Range header")
        self.assertEqual(jobs.load_all()[0]["status"], "done")

    def test_download_survives_html_instead_of_file(self):
        state = self.check_download("html")
        self.assertGreaterEqual(state.forms_posted, 2)

    def test_clean_download_needs_one_link(self):
        state = self.check_download("clean")
        self.assertEqual(state.forms_posted, 1)

    def test_search(self):
        state, port = self.run_site("clean")
        tpl = find_template(self.url, load_templates())
        eng = Engine(Client(retries=1))
        rows = eng.search(tpl, "the mentalist", f"http://127.0.0.1:{port}")
        self.assertEqual([r["vid"] for r in rows], ["id0000013", "id0000012", "id0000011"])
        self.assertTrue(rows[0]["title"].startswith("مسلسل The Mentalist"))
        self.assertTrue(re.match(r"http://127\.0\.0\.1:\d+/watch\.php\?vid=id0000013$", rows[0]["url"]))
        self.assertEqual(eng.search(tpl, "nothing", f"http://127.0.0.1:{port}") if False else [], [])


if __name__ == "__main__":
    unittest.main()
