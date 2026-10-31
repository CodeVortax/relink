"""Find sites worth writing templates for, study them, and pack what a template needs.

Pipeline (`relink discover`):
  1. search   run queries on DuckDuckGo / Bing (no API key), collect candidate sites.
              Or skip search and give sites yourself (--site / --urls).
  2. study    fetch each home page and fingerprint it: CMS and theme, search form,
              watch/download links, embed hosts, Cloudflare, parked pages.
  3. group    sites that share a CMS+theme go in one family. Most mirror/clone sites run the
              same theme, so ONE template usually covers a whole family.
  4. bundle   for the best sites save trimmed sample pages (home, search, watch, download)
              into one small .tar.gz plus report.md. Send those back to build templates.

Standard library only. Results are saved after every site, so a dropped connection
costs nothing: run the same command again and it continues.
"""
import base64
import io
import json
import re
import tarfile
import time
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

from .net import Blocked, NetError

# not streaming sites: never worth a template
EXCLUDE = {
    "google.com", "bing.com", "duckduckgo.com", "yahoo.com", "youtube.com", "youtu.be",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "reddit.com",
    "wikipedia.org", "imdb.com", "themoviedb.org", "rottentomatoes.com", "netflix.com",
    "amazon.com", "primevideo.com", "disneyplus.com", "hulu.com", "hbomax.com", "max.com",
    "apple.com", "microsoft.com", "pinterest.com", "linkedin.com", "quora.com", "medium.com",
    "github.com", "fandom.com", "tvmaze.com", "trakt.tv", "letterboxd.com", "t.me",
    "telegram.org", "whatsapp.com", "vimeo.com", "dailymotion.com", "twitch.tv", "wa.me",
    "zhihu.com", "yahoo.co.jp", "zoominfo.com", "rocketreach.co", "brainyquote.com",
    "goodreads.com", "tumblr.com", "wordpress.com", "blogspot.com", "quora.com", "baidu.com",
    "yandex.com", "naver.com", "ebay.com", "alibaba.com", "tripadvisor.com", "cnn.com",
    "bbc.com", "nytimes.com", "variety.com", "deadline.com", "ign.com", "metacritic.com",
}

# domains that show up in iframes/scripts but are never the video host
NOISE_HOSTS = ("google", "gstatic", "facebook", "twitter", "youtube", "doubleclick", "cloudflare",
               "jquery", "bootstrap", "fontawesome", "googletagmanager", "disqus", "gravatar",
               "wp.com", "w.org", "addthis", "sharethis", "recaptcha", "histats", "yandex")

# file hosts that sites embed; seeing them in a page tells us which adapters matter
KNOWN_HOSTS = [
    "mixdrop", "doodstream", "dood.", "streamtape", "vidmoly", "voe.sx", "filemoon",
    "uqload", "vipserver", "liiivideo", "abstream", "minochinos", "hgcloud", "bysebuho",
    "streamwish", "vidhide", "upstream", "ok.ru", "mp4upload", "vidoza", "supervideo",
    "lulustream", "streamsb", "sbembed", "krakenfiles", "gofile", "pixeldrain",
    "1fichier", "mega.nz", "drive.google", "cloudvideo", "fembed", "jwplayer",
]

WATCH_RE = re.compile(
    r"watch|episode|season|series|movie|film|/play|/vid|stream|show|anime|"
    r"مسلسل|فيلم|حلقة|الحلقة|موسم|مشاهدة|انمي", re.I)
EPISODE_RE = re.compile(r"episode|season|\bs\d{1,2}e\d{1,2}\b|\bep-?\d+|حلقة|الحلقة|موسم|الموسم", re.I)
DOWNLOAD_RE = re.compile(r"download|/dl\b|/d/|get-?link|تحميل|تنزيل", re.I)
SEARCH_FIELDS = {"s", "q", "query", "search", "keyword", "keywords", "k", "story", "term"}
PARKED_RE = re.compile(r"domain is for sale|buy this domain|parked|this domain may be for sale"
                       r"|sedo\.com|hugedomains|دومين للبيع", re.I)
MEDIA_RE = re.compile(r"m3u8|\.mp4|embed|token|source|sources|file\s*:|iframe|data-embed|download", re.I)


# ---------------------------------------------------------------- search engines

def host_of(url):
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def site_key(url):
    """Unique, file-name-safe name of a site (keeps the port, so local test sites differ)."""
    n = urlsplit(url).netloc.lower()
    return (n[4:] if n.startswith("www.") else n).replace(":", "_")


def excluded(url):
    h = host_of(url)
    return any(h == d or h.endswith("." + d) for d in EXCLUDE)


def _attr(tag, name):
    m = re.search(r'(?<![\w-])' + name + r'\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', tag, re.I)
    if not m:
        return ""
    return unescape(m.group(1) if m.group(1) is not None else m.group(2))


def parse_ddg(html):
    """Result links from html.duckduckgo.com/html. They are wrapped as /l/?uddg=<real url>."""
    out = []
    for m in re.finditer(r"<a\b[^>]*>", html, re.I):
        tag = m.group(0)
        if "result__a" not in _attr(tag, "class"):
            continue
        href = _attr(tag, "href")
        if "uddg=" in href:
            href = unquote(parse_qs(urlsplit(href).query).get("uddg", [""])[0])
        elif href.startswith("//"):
            href = "https:" + href
        if href.startswith("http"):
            out.append(href)
    return out


def _bing_unwrap(href):
    """Bing wraps results as /ck/a?...&u=a1<base64url of the real url>."""
    if "/ck/a" in href:
        u = parse_qs(urlsplit(href).query).get("u", [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return ""
    return href


def parse_bing(html):
    out = []
    for blk in re.findall(r'<li\b[^>]*class="[^"]*b_algo[^"]*"[^>]*>(.*?)</li>', html, re.S | re.I):
        m = re.search(r"<a\b[^>]*>", blk, re.I)
        if m:
            href = _bing_unwrap(_attr(m.group(0), "href"))
            if href.startswith("http"):
                out.append(href)
    return out


def search_engine(client, query, engine="both"):
    """URLs found for one query. With "both", DuckDuckGo and Bing results are merged."""
    order = {"ddg": ["ddg"], "bing": ["bing"], "both": ["ddg", "bing"]}[engine]
    found = []
    for name in order:
        try:
            if name == "ddg":
                r = client.request("https://html.duckduckgo.com/html/", data="q=" + quote(query))
                got = parse_ddg(r.text)
            else:
                r = client.request("https://www.bing.com/search?q=" + quote(query) + "&count=30")
                got = parse_bing(r.text)
        except (NetError, Blocked):
            got = []
        found += [u for u in got if u not in found]
    return found


def expand_title(title):
    """One title -> queries that surface episode/streaming sites rather than articles."""
    return [
        f"{title} season 1 episode 1 watch online -quotes -wiki -imdb",
        f"{title} S01E01 download -quotes -wiki -imdb",
        f"{title} الموسم الاول الحلقة 1 مترجم مشاهدة",
        f"{title} الحلقة 1 تحميل مباشر",
        f"مسلسل {title} كامل اون لاين",
    ]


def candidates_from_search(client, queries, engine="both", log=print, pause=2.0):
    """Unique site origins in the order they were found, with how many queries hit each."""
    seen = {}
    for q in queries:
        urls = search_engine(client, q, engine)
        log(f"  search: {q!r} -> {len(urls)} results")
        for u in urls:
            if excluded(u):
                continue
            sp = urlsplit(u)
            origin = f"{sp.scheme}://{sp.netloc}"
            seen.setdefault(origin, {"origin": origin, "hits": 0, "example": u})["hits"] += 1
        time.sleep(pause)
    return sorted(seen.values(), key=lambda c: -c["hits"])


# ---------------------------------------------------------------- fingerprint

def clean(s, n=90):
    s = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", s))).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def detect_cms(html):
    low = html.lower()
    theme = re.search(r"/wp-content/themes/([\w.-]+)/", html)
    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if "wp-content" in low or "wp-json" in low:
        return "wordpress", theme.group(1) if theme else ""
    if "datalife engine" in low or "dle_root" in low or "dle_login_hash" in low:
        return "dle", ""
    if "blogger.com" in low and "blogspot" in low:
        return "blogger", ""
    if gen:
        return gen.group(1).split()[0].lower(), theme.group(1) if theme else ""
    return "unknown", theme.group(1) if theme else ""


def find_search_form(html, base):
    for m in re.finditer(r"(<form\b[^>]*>)(.*?)(?:</form>|\Z)", html, re.I | re.S):
        for inp in re.finditer(r"<input\b[^>]*>", m.group(2), re.I):
            name = _attr(inp.group(0), "name").lower()
            if name in SEARCH_FIELDS:
                return {
                    "action": urljoin(base, _attr(m.group(1), "action") or base),
                    "method": (_attr(m.group(1), "method") or "get").lower(),
                    "param": name,
                }
    return None


def classify_links(html, base):
    """Same-site links that look like watch pages / download pages, with their anchor text."""
    host = host_of(base)
    watch, dl, seen = [], [], set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", html, re.I | re.S):
        href = _attr("<a " + m.group(1) + ">", "href")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urljoin(base, href)
        if url in seen or urlsplit(url).path in ("", "/"):
            continue
        seen.add(url)
        text = clean(m.group(2), 60)
        same = host_of(url) == host
        if DOWNLOAD_RE.search(url) or DOWNLOAD_RE.search(text):
            dl.append({"url": url, "text": text, "same_site": same})
        elif same and (WATCH_RE.search(urlsplit(url).path + "?" + urlsplit(url).query)
                       or WATCH_RE.search(text)):
            watch.append({"url": url, "text": text})
    return watch, dl


def known_hosts(html):
    low = html.lower()
    return sorted({k.strip(".") for k in KNOWN_HOSTS if k in low})


def embed_hosts(html):
    hosts = set()
    for m in re.finditer(r"(?:<iframe[^>]+src|data-embed|data-iframe)\s*=\s*[\"'][^\"']*?"
                         r"(?:https?:)?//([\w.-]+\.[a-z]{2,})", html, re.I):
        h = m.group(1).lower()
        if not any(n in h for n in NOISE_HOSTS):
            hosts.add(h)
    return sorted(hosts | set(known_hosts(html)))


def fingerprint(client, origin):
    fp = {"origin": origin, "ok": False}
    try:
        r = client.request(origin + "/")
    except Blocked:
        fp.update(error="cloudflare challenge", challenge=True)
        return fp
    except NetError as e:
        fp.update(error=str(e)[:120])
        return fp
    html = r.text
    fp.update(status=r.status, final=r.url, size=len(html))
    if r.status >= 400:
        fp["error"] = f"HTTP {r.status}"
        return fp
    cms, theme = detect_cms(html)
    watch, dl = classify_links(html, r.url)
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    lang = re.search(r"<html[^>]+lang=[\"']?([\w-]+)", html, re.I)
    fp.update(
        ok=True,
        title=clean(m.group(1), 80) if m else "",
        lang=(lang.group(1).lower() if lang else ""),
        cms=cms, theme=theme,
        search=find_search_form(html, r.url),
        watch_links=len(watch), download_links=len(dl),
        sample_watch=watch[:5], sample_download=dl[:3],
        embeds=embed_hosts(html),
        video_hosts=known_hosts(html),
        episode_links=sum(1 for w in watch if EPISODE_RE.search(w["url"] + " " + w["text"])),
        parked=bool(PARKED_RE.search(html[:20000])) or (len(watch) + len(dl) == 0 and len(html) < 3000),
        moved=host_of(r.url) != host_of(origin),
    )
    fp["family"] = family_of(fp)
    fp["score"] = score(fp)
    return fp


def family_of(fp):
    if fp.get("cms") in (None, "unknown") and not fp.get("theme"):
        return "unknown"
    return fp["cms"] + (":" + fp["theme"] if fp.get("theme") else "")


def score(fp):
    """Rough usefulness: how likely a template gets us from this site to a direct file.
    Without video evidence (episode-style links or a known video host) a site scores at most 1,
    so forums, quote sites and directories never make it into the bundle."""
    if not fp.get("ok") or fp.get("parked"):
        return 0
    video = bool(fp.get("video_hosts")) or fp.get("episode_links", 0) >= 2
    if not video:
        return 1 if fp.get("watch_links") else 0
    s = 2
    s += 3 if fp.get("search") else 0
    s += 1 if fp.get("download_links") else 0
    s += 2 if fp.get("embeds") else 0
    s += 1 if fp.get("episode_links", 0) >= 5 else 0
    return s


def group_families(fps):
    fams = {}
    for fp in fps:
        fams.setdefault(fp.get("family") or "unreachable", []).append(fp)
    return dict(sorted(fams.items(), key=lambda kv: -len(kv[1])))


# ---------------------------------------------------------------- trimming and bundling

def trim_html(html, cap=40000):
    """Keep structure, forms, links and the scripts that carry media info. Drop the rest."""
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    html = re.sub(r"<(style|svg|noscript)\b.*?</\1>", "", html, flags=re.S | re.I)
    html = re.sub(r"data:[\w/+.-]+;base64,[A-Za-z0-9+/=]+", "data:…", html)

    def script(m):
        body = m.group(2)
        if m.group(1).strip().find("src=") >= 0 and not body.strip():
            return m.group(0)
        if MEDIA_RE.search(body):
            return f"<script{m.group(1)}>{body[:2500]}</script>"
        return f"<script{m.group(1)}>…</script>"

    html = re.sub(r"<script\b([^>]*)>(.*?)</script>", script, html, flags=re.S | re.I)
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\s*\n\s*", "\n", html)
    return html[:cap]


def sample_pages(client, fp, query, log=print):
    """Fetch the pages a template author needs: home, search, a watch page, a download page."""
    pages = {}
    base = fp.get("final") or fp["origin"]

    def grab(name, url, data=None):
        try:
            r = client.request(url, data=data, referer=base)
            pages[name] = {"url": r.url, "status": r.status, "html": trim_html(r.text)}
            return r
        except (NetError, Blocked) as e:
            log(f"    {name}: {str(e)[:80]}")
            return None

    grab("home", base)
    s = fp.get("search")
    if s:
        if s["method"] == "post":
            grab("search", s["action"], f"{s['param']}={quote(query)}")
        else:
            sep = "&" if "?" in s["action"] else "?"
            grab("search", f"{s['action']}{sep}{s['param']}={quote(query)}")
    watch = fp.get("sample_watch") or []
    # prefer a deeper path (an episode or movie page) over a category page
    watch = sorted(watch, key=lambda w: -urlsplit(w["url"]).path.count("/"))
    r = grab("watch", watch[0]["url"]) if watch else None
    if r is not None:
        _, dl = classify_links(r.text, r.url)
        dl = [d for d in dl if d["same_site"]] or dl
        if dl:
            grab("download", dl[0]["url"])
    elif fp.get("sample_download"):
        grab("download", fp["sample_download"][0]["url"])
    return pages


def write_report(fps, fams, picked, path):
    L = ["# relink discover report", ""]
    L.append(f"{len(fps)} sites studied, {sum(1 for f in fps if f.get('ok'))} reachable, "
             f"{len(fams)} families.\n")
    L.append("## Families (one template can often cover a whole family)")
    for fam, members in fams.items():
        L.append(f"- **{fam}**: " + ", ".join(site_key(m["origin"]) for m in members[:8]))
    L.append("\n## Sites, best first")
    L.append("score | site | family | search | watch/dl links | embeds | note")
    L.append("--- | --- | --- | --- | --- | --- | ---")
    for f in sorted(fps, key=lambda x: -x.get("score", 0)):
        note = f.get("error") or ("parked" if f.get("parked") else
                                  ("moved to " + host_of(f["final"]) if f.get("moved") else ""))
        sf = f.get("search")
        L.append(f"{f.get('score', 0)} | {site_key(f['origin'])} | {f.get('family', '-')} | "
                 f"{(sf['param'] + ' ' + sf['method']) if sf else '-'} | "
                 f"{f.get('watch_links', 0)}/{f.get('download_links', 0)} | "
                 f"{', '.join(f.get('embeds', [])[:4])} | {note}")
    L.append("\n## In the bundle")
    L += [f"- {site_key(f['origin'])}" for f in picked]
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def write_bundle(outdir, report_path, site_pages):
    """One .tar.gz: report.md + <site>/<page>.html (trimmed) + <site>/info.json."""
    tgz = Path(outdir) / "relink-discover.tar.gz"
    with tarfile.open(tgz, "w:gz") as tar:
        tar.add(report_path, arcname="report.md")
        for host, (fp, pages) in site_pages.items():
            info = {k: fp.get(k) for k in ("origin", "final", "family", "cms", "theme", "lang",
                                           "search", "embeds", "score", "title")}
            info["pages"] = {n: {"url": p["url"], "status": p["status"]} for n, p in pages.items()}
            blobs = {"info.json": json.dumps(info, ensure_ascii=False, indent=1)}
            blobs.update({f"{n}.html": p["html"] for n, p in pages.items()})
            for name, text in blobs.items():
                data = text.encode("utf-8")
                ti = tarfile.TarInfo(f"{host}/{name}")
                ti.size = len(data)
                ti.mtime = int(time.time())
                tar.addfile(ti, io.BytesIO(data))
    return tgz


# ---------------------------------------------------------------- the whole pipeline

def run(client, outdir, queries=(), sites=(), engine="both", top=6, query="love",
        refresh=False, log=print, pause=2.0):
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    state_path = out / "fingerprints.json"
    state = {}
    if state_path.exists() and not refresh:
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except ValueError:
            state = {}

    cands = [{"origin": o.rstrip("/"), "hits": 0, "example": o} for o in sites]
    if queries:
        log(f"searching ({engine}) ...")
        cands += candidates_from_search(client, list(queries), engine, log, pause)
    seen, uniq = set(), []
    for c in cands:
        if c["origin"] not in seen:
            seen.add(c["origin"])
            uniq.append(c)
    if not uniq:
        raise NetError("no sites to study. The search engines gave nothing (they may block this "
                       "network): pass sites yourself with --site https://example.com or --urls FILE")
    log(f"{len(uniq)} candidate sites")

    for i, c in enumerate(uniq, 1):
        if c["origin"] in state:
            continue
        log(f"[{i}/{len(uniq)}] studying {site_key(c['origin'])}")
        fp = fingerprint(client, c["origin"])
        fp["hits"] = c["hits"]
        state[c["origin"]] = fp
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    fps = [state[c["origin"]] for c in uniq if c["origin"] in state]
    fams = group_families(fps)
    best = [f for f in sorted(fps, key=lambda x: (-x.get("score", 0), -x.get("hits", 0)))
            if f.get("score", 0) >= 3]
    # at most two sites per family, so the bundle shows variety, not ten clones
    picked, per = [], {}
    for f in best:
        k = f.get("family")
        if per.get(k, 0) < 2:
            picked.append(f)
            per[k] = per.get(k, 0) + 1
        if len(picked) >= top:
            break

    site_pages = {}
    for f in picked:
        log(f"sampling pages of {site_key(f['origin'])}")
        pages = sample_pages(client, f, query, log=lambda m: log(m))
        if pages:
            site_pages[site_key(f["origin"])] = (f, pages)
    report = out / "report.md"
    write_report(fps, fams, picked, report)
    tgz = write_bundle(out, report, site_pages)
    return {"fingerprints": fps, "families": fams, "picked": picked, "report": report,
            "bundle": tgz}
