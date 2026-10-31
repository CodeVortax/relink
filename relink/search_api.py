"""A small web-search API over several engines. Standard library only.

    from relink.search_api import search
    out = search("the mentalist s01e01", engines=["google", "ddg", "bing"], lang="ar")
    for r in out["results"]:
        print(r["title"], r["url"], r["engines"])
    print(out["engines"])      # per engine: ok / count / error, so a blocked engine is visible

Engines: google (basic no-JS page), ddg (html.duckduckgo.com), bing, brave, mojeek.
Results of all engines are merged (same page found by several engines ranks higher).
Nothing here needs an API key. Search engines change their pages and block scripts from time
to time: each engine has its own parser plus a generic fallback parser, and an engine that
fails never stops the others. Use it politely (a short pause between requests is built in).

Also usable as a command (`relink web ...`) and as a local JSON API (`relink web --serve`).
"""
import base64
import json
import re
import time
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from .net import Blocked, Client, NetError


def text(s, n=300):
    s = re.sub(r"</?(?:b|strong|em|i|span|mark|wbr)\b[^>]*>", "", s or "")   # inline: no gap
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", unescape(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def host_of(url):
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def clean_url(href):
    """Unwrap the redirect links engines put around results; None if not a real http(s) URL."""
    href = unescape(href or "").strip()
    if href.startswith("//"):
        href = "https:" + href
    q = parse_qs(urlsplit(href).query)
    if "uddg=" in href:                                       # duckduckgo
        href = q.get("uddg", [""])[0]
    elif href.startswith("/url?") or "google." in host_of(href) and "/url" in href:
        href = (q.get("q") or q.get("url") or [""])[0]      # google
    elif "/ck/a" in href:                                     # bing: u=a1<base64url>
        u = (q.get("u") or [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            try:
                href = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return None
    return href if href.startswith(("http://", "https://")) else None


def generic_parse(html, own_hosts=()):
    """Fallback: external links that carry a heading (h2/h3) are results on nearly every engine."""
    out = []
    anchors = list(re.finditer(r"<a\b[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.S | re.I))
    for i, m in enumerate(anchors):
        url = clean_url(m.group(1))
        h = re.search(r"<h[23][^>]*>(.*?)</h[23]>", m.group(2), re.S | re.I)
        if not url or not h or any(o in host_of(url) for o in own_hosts):
            continue
        end = m.end()
        nxt = anchors[i + 1].start() if i + 1 < len(anchors) else end + 1500
        out.append({"title": text(h.group(1), 160), "url": url,
                    "snippet": text(html[end:min(nxt, end + 1500)], 220)})
    return out


class Engine:
    name = ""
    hosts = ()
    headers = {}
    blocked = ()            # lower-case marks of a captcha / consent page

    def request(self, q, page, lang, region):          # -> (url, post data or None)
        raise NotImplementedError

    def parse(self, html):
        return []


class Google(Engine):
    name, hosts = "google", ("google.",)
    headers = {"Cookie": "CONSENT=YES+cb"}
    blocked = ("unusual traffic", "/sorry/", "enablejs", "consent.google.com")

    def request(self, q, page, lang, region):
        p = {"q": q, "gbv": 1, "num": 10, "start": page * 10}
        if lang:
            p["hl"] = lang
        if region:
            p["gl"] = region
        return "https://www.google.com/search?" + urlencode(p), None

    def parse(self, html):
        out = []
        anchors = list(re.finditer(r"<a\b[^>]*href=\"(/url\?[^\"]+)\"[^>]*>(.*?)</a>", html, re.S))
        for i, m in enumerate(anchors):
            url = clean_url(m.group(1))
            h3 = re.search(r"<h3[^>]*>(.*?)</h3>", m.group(2), re.S)
            title = text(h3.group(1) if h3 else m.group(2), 160)
            if not url or not title or "google." in host_of(url):
                continue
            end = m.end()
            nxt = anchors[i + 1].start() if i + 1 < len(anchors) else end + 1500
            out.append({"title": title, "url": url,
                        "snippet": text(html[end:min(nxt, end + 1500)], 220)})
        return out


class DuckDuckGo(Engine):
    name, hosts = "ddg", ("duckduckgo.",)
    blocked = ("anomaly-modal", "bots use duckduckgo", "unfortunately, bots")

    def request(self, q, page, lang, region):
        d = {"q": q}
        if region:
            d["kl"] = f"{region.lower()}-{(lang or 'en').lower()}"
        if page:
            d.update(s=page * 30, dc=page * 30 + 1)
        return "https://html.duckduckgo.com/html/", urlencode(d)

    def parse(self, html):
        out = []
        anchors = [m for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, re.S)
                   if "result__a" in m.group(1)]
        for i, m in enumerate(anchors):
            href = re.search(r"href=\"([^\"]+)\"", m.group(1))
            url = clean_url(href.group(1)) if href else None
            if not url:
                continue
            nxt = anchors[i + 1].start() if i + 1 < len(anchors) else len(html)
            sn = re.search(r"class=\"result__snippet\"[^>]*>(.*?)</a>", html[m.end():nxt], re.S)
            out.append({"title": text(m.group(2), 160), "url": url,
                        "snippet": text(sn.group(1), 220) if sn else ""})
        return out


class Bing(Engine):
    name, hosts = "bing", ("bing.", "microsoft.")
    blocked = ("captcha", "unusual traffic")

    def request(self, q, page, lang, region):
        p = {"q": q, "first": page * 10 + 1, "count": 10}
        if lang:
            p["setlang"] = lang
        if region:
            p["cc"] = region
        return "https://www.bing.com/search?" + urlencode(p), None

    def parse(self, html):
        out = []
        for blk in re.findall(r"<li\b[^>]*class=\"[^\"]*b_algo[^\"]*\"[^>]*>(.*?)</li>", html, re.S):
            a = re.search(r"<h2[^>]*>.*?<a\b[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", blk, re.S)
            url = clean_url(a.group(1)) if a else None
            if not url:
                continue
            p = re.search(r"<p\b[^>]*>(.*?)</p>", blk, re.S)
            out.append({"title": text(a.group(2), 160), "url": url,
                        "snippet": text(p.group(1), 220) if p else ""})
        return out


class Brave(Engine):
    name, hosts = "brave", ("brave.",)
    blocked = ("captcha", "rate limit")

    def request(self, q, page, lang, region):
        p = {"q": q, "source": "web"}
        if page:
            p["offset"] = page
        return "https://search.brave.com/search?" + urlencode(p), None

    def parse(self, html):
        out = []
        for blk in re.split(r"<div[^>]+class=\"[^\"]*\bsnippet\b[^\"]*\"", html)[1:]:
            a = re.search(r"<a\b[^>]*href=\"(https?://[^\"]+)\"[^>]*>(.*?)</a>", blk, re.S)
            if not a or "brave." in host_of(a.group(1)):
                continue
            t = re.search(r"class=\"[^\"]*\btitle\b[^\"]*\"[^>]*>(.*?)</", blk, re.S)
            d = re.search(r"class=\"[^\"]*(?:snippet-description|content)[^\"]*\"[^>]*>(.*?)</", blk, re.S)
            out.append({"title": text(t.group(1) if t else a.group(2), 160), "url": a.group(1),
                        "snippet": text(d.group(1), 220) if d else ""})
        return out


class Mojeek(Engine):
    name, hosts = "mojeek", ("mojeek.",)
    blocked = ("captcha", "403 forbidden")

    def request(self, q, page, lang, region):
        p = {"q": q, "s": page * 10 + 1}
        if lang:
            p["lb"] = lang
        if region:
            p["arc"] = region
        return "https://www.mojeek.com/search?" + urlencode(p), None

    def parse(self, html):
        out = []
        for blk in re.findall(r"<li\b[^>]*>(.*?)</li>", html, re.S):
            a = re.search(r"<a\b[^>]*class=\"[^\"]*\btitle\b[^\"]*\"[^>]*>", blk)
            if not a:
                continue
            href = re.search(r"href=\"([^\"]+)\"", a.group(0))
            url = clean_url(href.group(1)) if href else None
            title = re.search(r"class=\"[^\"]*\btitle\b[^\"]*\"[^>]*>(.*?)</a>", blk, re.S)
            s = re.search(r"<p\b[^>]*class=\"[^\"]*\bs\b[^\"]*\"[^>]*>(.*?)</p>", blk, re.S)
            if url:
                out.append({"title": text(title.group(1), 160) if title else url, "url": url,
                            "snippet": text(s.group(1), 220) if s else ""})
        return out


ENGINES = {e.name: e for e in (Google(), DuckDuckGo(), Bing(), Brave(), Mojeek())}
DEFAULT = ("google", "ddg", "bing")


def norm(url):
    sp = urlsplit(url)
    return host_of(url) + sp.path.rstrip("/") + ("?" + sp.query if sp.query else "")


def merge(per_engine, k=60):
    """Reciprocal rank fusion: a page several engines return, and high up, ranks first."""
    by = {}
    for name, rows in per_engine.items():
        seen = set()
        for rank, r in enumerate(rows):
            key = norm(r["url"])
            if key in seen:
                continue
            seen.add(key)
            e = by.setdefault(key, {**r, "engines": [], "score": 0.0})
            e["engines"].append(name)
            e["score"] += 1.0 / (k + rank)
            if len(r["snippet"]) > len(e["snippet"]):
                e["snippet"] = r["snippet"]
    rows = sorted(by.values(), key=lambda r: -r["score"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
        r["score"] = round(r["score"], 5)
    return rows


def search(query, engines=DEFAULT, pages=1, lang=None, region=None, limit=None,
           client=None, pause=1.0, timeout=20):
    """Run `query` on each engine and return {"query", "results", "engines"}.

    results  merged list of {rank, title, url, snippet, engines, score}
    engines  {name: {"ok", "count", "error"}}: says which engines worked and why one did not
    client   anything with .request(url, data=None, referer=None) -> obj with .text (tests)
    """
    if isinstance(engines, str):
        engines = [e.strip() for e in engines.split(",") if e.strip()]
    engines = list(engines)
    unknown = [e for e in engines if e not in ENGINES]
    if unknown:
        raise ValueError(f"unknown engine {unknown[0]!r}; choose from {', '.join(ENGINES)}")
    per, report = {}, {}
    for name in engines:
        eng = ENGINES[name]
        c = client or Client(headers=eng.headers, timeout=timeout, retries=2)
        rows, error = [], None
        for pg in range(max(1, pages)):
            url, data = eng.request(query, pg, lang, region)
            try:
                r = c.request(url, data=data)
            except (NetError, Blocked) as e:
                error = str(e)[:120]
                break
            got = eng.parse(r.text) or generic_parse(r.text, eng.hosts)
            if not got:
                low = r.text[:30000].lower()
                error = ("blocked (captcha or consent page)" if any(m in low for m in eng.blocked)
                         else f"no results found (HTTP {r.status})")
                break
            rows += got
            if pg + 1 < pages:
                time.sleep(pause)
        per[name] = rows
        report[name] = {"ok": bool(rows), "count": len(rows), "error": None if rows else error}
        time.sleep(pause if len(engines) > 1 and name != engines[-1] else 0)
    results = merge(per)
    return {"query": query, "results": results[:limit] if limit else results, "engines": report}


# ---------------------------------------------------------------- local JSON API

def make_server(port=8765, host="127.0.0.1", search_fn=search):
    """GET /search?q=...&engines=google,ddg&pages=1&lang=ar&region=eg&limit=20  ->  JSON."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            sp = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(sp.query).items()}
            if sp.path == "/search" and q.get("q"):
                try:
                    out = search_fn(q["q"], engines=q.get("engines", ",".join(DEFAULT)),
                                    pages=int(q.get("pages", 1)), lang=q.get("lang"),
                                    region=q.get("region"),
                                    limit=int(q["limit"]) if q.get("limit") else None)
                except ValueError as e:
                    return self.reply(400, {"error": str(e)})
                return self.reply(200, out)
            self.reply(200, {"usage": "/search?q=QUERY&engines=google,ddg,bing,brave,mojeek"
                                      "&pages=1&lang=ar&region=eg&limit=20",
                             "engines": list(ENGINES)})

    return ThreadingHTTPServer((host, port), H)
