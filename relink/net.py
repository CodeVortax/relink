"""Small HTTP client: cookies, retries, gzip, clear errors. Standard library only."""
import gzip
import time
import zlib
from http.client import HTTPException
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener

UA = ("Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36")

RETRY_STATUS = {429, 500, 502, 503, 504}


class NetError(Exception):
    pass


class Blocked(NetError):
    """The site answered with a Cloudflare challenge instead of the page."""


def safe_url(url):
    """Percent-encode what urllib cannot send (spaces, Arabic) but keep [] and %XX as they are."""
    return quote(url, safe="%:/?&=+,;@!$*'()~#[]-._")


class Response:
    def __init__(self, status, url, headers, body):
        self.status, self.url, self.headers, self.body = status, url, headers, body

    @property
    def text(self):
        charset = self.headers.get_content_charset() or "utf-8"
        return self.body.decode(charset, errors="replace")


class Client:
    def __init__(self, headers=None, timeout=60, retries=5, cookie=None,
                 log=lambda m: None, sleep=time.sleep):
        self.jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.jar))
        self.timeout, self.retries, self.log, self.sleep = timeout, retries, log, sleep
        self.headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
        }
        self.headers.update(headers or {})
        if cookie:
            self.headers["Cookie"] = cookie

    def request(self, url, data=None, referer=None, ajax=False):
        """GET, or POST when data (a urlencoded string) is given. Retries network trouble."""
        headers = dict(self.headers)
        if referer:
            headers["Referer"] = referer
        body = None
        if data is not None:
            body = data.encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        if ajax:
            headers["X-Requested-With"] = "XMLHttpRequest"

        last = None
        for attempt in range(1, self.retries + 1):
            try:
                req = Request(safe_url(url), data=body, headers=headers)
                try:
                    resp = self.opener.open(req, timeout=self.timeout)
                except HTTPError as e:
                    resp = e
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    raw = zlib.decompress(raw)
                status = getattr(resp, "status", None) or resp.code
                out = Response(status, resp.geturl(), resp.headers, raw)
                if status in (403, 503) and b"Just a moment" in raw[:4000]:
                    raise Blocked(
                        "Cloudflare challenge. Open the site in your browser, copy the "
                        "cf_clearance cookie and pass it with --cookie 'cf_clearance=...'")
                if status in RETRY_STATUS and attempt < self.retries:
                    last = f"HTTP {status}"
                    self.log(f"  {url}: HTTP {status}, retrying")
                    self.sleep(min(3 * attempt, 20))
                    continue
                return out
            except Blocked:
                raise
            except (URLError, OSError, HTTPException, zlib.error, EOFError) as e:
                last = e
                self.log(f"  network trouble ({e}); try {attempt}/{self.retries}")
                self.sleep(min(3 * attempt, 20))
        raise NetError(f"{url}: {last}")
