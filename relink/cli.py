"""relink command line."""
import argparse
import sys
from pathlib import Path

from . import __version__, discover, generic, jobs
from .download import DownloadError, download, human
from .engine import Engine, ResolveError, TemplateError, find_template, load_templates
from .net import Blocked, Client, NetError

MEDIA_EXT = {".mp4", ".mkv", ".webm", ".ts", ".avi"}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def make_engine(tpl, cookie=None, retries=None):
    req = tpl.d.get("request") or {}
    client = Client(headers=req.get("headers"), timeout=req.get("timeout", 60),
                    retries=retries or req.get("retries", 5), cookie=cookie, log=log)
    return Engine(client, log)


def default_outdir():
    shared = Path.home() / "storage" / "downloads"
    return shared / "relink" if shared.is_dir() else Path("downloads")


def pick_dest(out, filename):
    if out:
        p = Path(out).expanduser()
        if p.suffix.lower() in MEDIA_EXT:
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        p.mkdir(parents=True, exist_ok=True)
        return p / filename
    d = default_outdir()
    d.mkdir(parents=True, exist_ok=True)
    return d / filename


def do_generic(url, quality, out, dry):
    """No template for this site: let yt-dlp do it (resumes, retries forever)."""
    generic.need_ytdlp()
    if dry:
        return generic.run(url, quality, ".", dry=True)
    outdir = Path(out).expanduser() if out else default_outdir()
    outdir.mkdir(parents=True, exist_ok=True)
    jid = jobs.job_id(url, quality)
    job = {"id": jid, "url": url, "quality": quality, "engine": "ytdlp",
           "dest": str(outdir), "title": url, "status": "downloading"}
    jobs.save(job)
    log("no template for this site, using yt-dlp (generic mode)")
    code = generic.run(url, quality, outdir)
    job["status"] = "done" if code == 0 else ("paused" if code == 130 else "failed")
    jobs.save(job)
    if code == 130:
        log("\nstopped. Run the same command or `relink resume` to continue.")
    return code


def do_get(url, quality="best", server=None, out=None, dry=False, cookie=None, retries=None,
           ytdlp=False):
    tpl = None if ytdlp else find_template(url, load_templates())
    if not tpl:
        return do_generic(url, quality, out, dry)
    eng = make_engine(tpl, cookie, retries)
    log(f"site: {tpl.name}")
    resolved = eng.resolve(tpl, url, server, quality)
    log(f"{resolved.title or resolved.filename}\n  server: {resolved.server}   "
        f"quality: {resolved.label or quality}")
    if dry:
        print(resolved.url)
        return 0

    dest = pick_dest(out, resolved.filename)
    jid = jobs.job_id(url, quality)
    job = {"id": jid, "url": url, "quality": quality, "server": server, "dest": str(dest),
           "title": resolved.title, "status": "downloading"}
    jobs.save(job)
    if dest.exists():
        log(f"already downloaded: {dest}")
        job["status"] = "done"
        jobs.save(job)
        return 0

    holder = {"r": resolved}

    def get_link(refresh):
        if not refresh:
            return holder["r"]
        try:
            holder["r"] = holder["r"].refresh()
        except (ResolveError, NetError) as e:
            log(f"  quick refresh failed ({e}); resolving again from the episode page")
            holder["r"] = eng.resolve(tpl, url, server, quality)
        return holder["r"]

    log(f"saving to: {dest}")
    try:
        download(get_link, str(dest), log=log)
    except KeyboardInterrupt:
        job["status"] = "paused"
        jobs.save(job)
        log("\nstopped. Run the same command or `relink resume` to continue.")
        return 130
    except (DownloadError, ResolveError, NetError) as e:
        job["status"] = "failed"
        job["error"] = str(e)
        jobs.save(job)
        raise
    job["status"] = "done"
    jobs.save(job)
    log(f"done: {dest}  ({human(dest.stat().st_size)})")
    return 0


def cmd_get(a):
    return do_get(a.url, a.quality, a.server, a.out, a.dry, a.cookie, a.retries, a.ytdlp)


def cmd_servers(a):
    tpl = find_template(a.url, load_templates())
    if not tpl:
        raise ResolveError("no template matches this URL")
    ctx, servers = make_engine(tpl, a.cookie).servers(tpl, a.url)
    hosts = tpl.hosts()
    print(ctx.get("title", "").strip() or a.url)
    for s in servers:
        ok = "supported" if s["name"].lower() in hosts else "no adapter yet"
        print(f"  {s['name']:<12} {ok:<15} {s['embed']}")
    return 0


def cmd_search(a):
    templates = [t for t in load_templates() if t.d.get("search")]
    if a.site:
        templates = [t for t in templates if t.name == a.site]
    if not templates:
        raise ResolveError("no template with search" + (f" named {a.site}" if a.site else ""))
    tpl = templates[0]
    rows = make_engine(tpl, a.cookie).search(tpl, " ".join(a.query), a.base)
    if not rows:
        print("no results")
    for r in rows:
        # the title gets its own line so a right-to-left title is never mixed with the id
        print(f"{r.get('title', '')}\n    {r.get('url', '')}")
    return 0


def cmd_discover(a):
    queries = list(a.query)
    for t in a.title or []:
        queries += discover.expand_title(t)
    sites = list(a.site or [])
    if a.urls:
        # any text works: a copied Google results page, a saved page, one URL per line
        sites += discover.urls_from_text(Path(a.urls).expanduser().read_text(encoding="utf-8",
                                                                             errors="replace"))
    sites = [s if "://" in s else "https://" + s for s in sites]
    if not queries and not sites:
        raise ResolveError("give something to search (a title or words) or sites with --site/--urls")
    client = Client(timeout=25, retries=2, cookie=a.cookie)
    res = discover.run(client, a.out, queries, sites, a.engine, a.top, a.probe_query, a.refresh, log)
    fps = res["fingerprints"]
    print(f"\n{len(fps)} sites studied, {sum(1 for f in fps if f.get('ok'))} reachable")
    for fam, members in list(res["families"].items())[:8]:
        print(f"  {fam}: {len(members)} site(s)")
    print(f"bundle: {res['bundle']}  ({res['bundle'].stat().st_size // 1024} KB)")
    print(f"report: {res['report']}\nSend me the bundle and I will write the templates.")
    return 0


def cmd_list(a):
    items = jobs.load_all()
    if not items:
        print("no jobs yet")
    for j in items:
        if j.get("engine") == "ytdlp":
            print(f"{j['id']}  {j['status']:<11} (yt-dlp) {j['url']}")
            continue
        part = Path(j["dest"] + ".part")
        size = f"  {human(part.stat().st_size)} so far" if part.exists() else ""
        print(f"{j['id']}  {j['status']:<11} {Path(j['dest']).name}{size}")
    return 0


def cmd_resume(a):
    code = 0
    for j in jobs.load_all():
        if j["status"] == "done":
            continue
        log(f"--- resuming {Path(j['dest']).name}")
        try:
            code |= do_get(j["url"], j["quality"], j.get("server"), j["dest"], cookie=a.cookie,
                           ytdlp=j.get("engine") == "ytdlp")
        except (DownloadError, ResolveError, NetError) as e:
            log(f"failed: {e}")
            code = 1
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relink", description="Download from sites with temporary links.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("get", help="download an episode/movie page")
    g.add_argument("url")
    g.add_argument("--quality", default="best", help="best (default), hd or low")
    g.add_argument("--server", help="force one server, e.g. vipserver")
    g.add_argument("--out", help="folder or file path (default: ~/storage/downloads/relink)")
    g.add_argument("--dry", action="store_true", help="only print the direct link")
    g.add_argument("--cookie", help="e.g. 'cf_clearance=...' if the site shows a challenge")
    g.add_argument("--retries", type=int)
    g.add_argument("--ytdlp", action="store_true", help="skip templates, use yt-dlp for this URL")
    g.set_defaults(fn=cmd_get)

    s = sub.add_parser("servers", help="list the servers of a page")
    s.add_argument("url")
    s.add_argument("--cookie")
    s.set_defaults(fn=cmd_servers)

    q = sub.add_parser("search", help="search a site")
    q.add_argument("query", nargs="+")
    q.add_argument("--site", help="template name")
    q.add_argument("--base", help="site origin, e.g. https://cima.my-cima.net")
    q.add_argument("--cookie")
    q.set_defaults(fn=cmd_search)

    d = sub.add_parser("discover", help="find and study sites so templates can be written")
    d.add_argument("query", nargs="*", help="search words, e.g. 'watch series online free'")
    d.add_argument("--title", action="append", help="a title; expands to several queries "
                   "(English and Arabic). Repeat it for more titles")
    d.add_argument("--site", action="append", help="study this site, no search (repeatable)")
    d.add_argument("--urls", help="text file with URLs, or any text that contains them (a copied Google results page)")
    d.add_argument("--engine", choices=["all", "google", "ddg", "bing", "both"], default="all")
    d.add_argument("--top", type=int, default=6, help="sites to sample pages from (default 6)")
    d.add_argument("--probe-query", default="love", help="word used to test each site's search")
    d.add_argument("--out", default="relink-discover", help="work folder (default relink-discover)")
    d.add_argument("--refresh", action="store_true", help="study again, ignore saved results")
    d.add_argument("--cookie")
    d.set_defaults(fn=cmd_discover)

    sub.add_parser("list", help="show jobs").set_defaults(fn=cmd_list)
    r = sub.add_parser("resume", help="continue unfinished jobs")
    r.add_argument("--cookie")
    r.set_defaults(fn=cmd_resume)

    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except Blocked as e:
        log(f"blocked: {e}")
    except (TemplateError, ResolveError, NetError, DownloadError) as e:
        log(f"error: {e}")
    except KeyboardInterrupt:
        log("\nstopped")
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
