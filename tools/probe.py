#!/usr/bin/env python3
"""relink probe: study one page of a site and print a compact report.

Usage:  python tools/probe.py <url> [--cookie "k=v; k2=v2"] [--referer URL] [--out DIR]

Stdlib only. Saves the raw HTML and a text report in DIR (default: probe-out/),
so the report can be pasted back for building a template.
"""
import argparse
import gzip
import re
import sys
import zlib
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

KEYWORDS = re.compile(
    r"download|watch|server|episode|season|token|embed|player|mirror|quality|"
    r"تحميل|مشاهدة|سيرفر|حلقة|الحلقة|موسم|الموسم|جودة",
    re.I,
)


class Tracker(HTTPRedirectHandler):
    def __init__(self):
        self.chain = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.chain.append((code, req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def read_body(resp):
    """Read in chunks; on a stall keep what arrived instead of losing it all."""
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    dec = None
    if enc == "gzip":
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif enc == "deflate":
        dec = zlib.decompressobj()
    parts, partial = [], False
    while True:
        try:
            chunk = resp.read(8192)
        except (TimeoutError, OSError) as e:
            print(f"warn: read stalled ({e}); keeping partial page", file=sys.stderr)
            partial = True
            break
        if not chunk:
            break
        try:
            parts.append(dec.decompress(chunk) if dec else chunk)
        except zlib.error:
            partial = True
            break
    return b"".join(parts), partial


def fetch(url, cookie=None, referer=None, timeout=90, retries=4):
    import time
    last = None
    for attempt in range(1, retries + 1):
        try:
            return _fetch_once(url, cookie, referer, timeout)
        except Exception as e:
            last = e
            wait = 3 * attempt
            print(f"attempt {attempt}/{retries} failed: {e}; retry in {wait}s", file=sys.stderr)
            time.sleep(wait)
    raise last


def _fetch_once(url, cookie=None, referer=None, timeout=90):
    tracker = Tracker()
    opener = build_opener(tracker)
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.8,ar;q=0.6",
        "Accept-Encoding": "gzip, deflate",
    }
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer
    req = Request(url, headers=headers)
    try:
        resp = opener.open(req, timeout=timeout)
        status = resp.status
    except Exception as e:  # HTTPError still has a body worth seeing
        resp = getattr(e, "fp", None) and e
        if resp is None:
            raise
        status = e.code
    raw, partial = read_body(resp)
    if partial:
        print("warn: page is incomplete", file=sys.stderr)
    charset = resp.headers.get_content_charset() or "utf-8"
    text = raw.decode(charset, errors="replace")
    return status, resp.geturl(), resp.headers, text, tracker.chain


def clean(s, n=160):
    s = re.sub(r"\s+", " ", unescape(s)).strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def report(url, status, final, headers, html, chain):
    out = []
    w = out.append
    w(f"URL      : {url}")
    w(f"STATUS   : {status}")
    w(f"FINAL    : {final}")
    for code, a, b in chain:
        w(f"REDIRECT : {code} {a} -> {b}")
    for h in ("Server", "Content-Type", "Set-Cookie", "CF-RAY", "Location"):
        v = headers.get_all(h)
        if v:
            w(f"HEADER   : {h}: {clean('; '.join(v), 200)}")
    low = html.lower()
    if "cf-chl" in low or "just a moment" in low or "cloudflare" in low and status in (403, 503):
        w("WARNING  : looks like a Cloudflare challenge page")
    w(f"SIZE     : {len(html)} chars")
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    w(f"TITLE    : {clean(m.group(1)) if m else '-'}")

    w("\n== IFRAMES ==")
    for m in re.finditer(r"<iframe\b[^>]*>", html, re.I):
        w("  " + clean(m.group(0), 300))

    w("\n== FORMS ==")
    for m in re.finditer(r"<form\b[^>]*>", html, re.I):
        w("  " + clean(m.group(0), 300))

    w("\n== SCRIPT SRC ==")
    for m in re.finditer(r"<script\b[^>]*\bsrc=[\"']([^\"']+)", html, re.I):
        w("  " + urljoin(final, m.group(1)))

    w("\n== INLINE SCRIPTS (with keywords) ==")
    for m in re.finditer(r"<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S | re.I):
        body = m.group(1)
        if KEYWORDS.search(body):
            w("  ---")
            w("  " + clean(body, 1200))

    w("\n== LINKS (keyword/Arabic labels) ==")
    seen = set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, re.S | re.I):
        attrs, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        hm = re.search(r"href=[\"']([^\"']*)", attrs, re.I)
        if not hm:
            continue
        href = urljoin(final, hm.group(1))
        if (KEYWORDS.search(label) or KEYWORDS.search(href)) and href not in seen:
            seen.add(href)
            w(f"  [{clean(label, 60)}] {href}")

    w("\n== DATA-* ATTRIBUTES ==")
    seen = set()
    for m in re.finditer(r"<(\w+)\b([^>]*\bdata-[\w-]+=[^>]*)>", html, re.I):
        s = clean(m.group(0), 260)
        if s not in seen:
            seen.add(s)
            w("  " + s)
        if len(seen) > 60:
            w("  ...")
            break

    w("\n== URLS IN PAGE (other hosts) ==")
    host = urlparse(final).netloc
    others = sorted({u for u in re.findall(r"https?://[^\s\"'<>\\)]+", html)
                     if urlparse(u).netloc != host})
    for u in others[:80]:
        w("  " + u)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--cookie")
    ap.add_argument("--referer")
    ap.add_argument("--out", default="probe-out")
    ap.add_argument("--timeout", type=int, default=90, help="seconds per network step")
    ap.add_argument("--retries", type=int, default=4)
    a = ap.parse_args()

    status, final, headers, html, chain = fetch(
        a.url, a.cookie, a.referer, a.timeout, a.retries)
    rep = report(a.url, status, final, headers, html, chain)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w.-]+", "_", urlparse(final).netloc + urlparse(final).path + "_" + urlparse(final).query)[:80]
    (out / f"{stem}.html").write_text(html, encoding="utf-8")
    (out / f"{stem}.report.txt").write_text(rep, encoding="utf-8")
    print(rep)
    print(f"\nSaved: {out}/{stem}.html and {out}/{stem}.report.txt", file=sys.stderr)


if __name__ == "__main__":
    main()
