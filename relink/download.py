"""Resumable downloader that survives dead links.

When the link expires (403/410...), a web page comes back instead of the file, or the
connection drops, it keeps the partial file, asks for a fresh link and continues from
the byte where it stopped. Standard library only.
"""
import os
import sys
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from .net import UA, safe_url

CHUNK = 64 * 1024
EXPIRED = {401, 403, 404, 410}


class DownloadError(Exception):
    pass


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024


def eta(seconds):
    if seconds is None or seconds < 0 or seconds > 99 * 3600:
        return "--"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


def _size(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def download(get_link, dest, *, log=print, stall=45, max_idle_refresh=4,
             sleep=time.sleep, progress=True):
    """get_link(refresh: bool) -> object with .url and .referer. Returns the final path."""
    part = dest + ".part"
    opener = build_opener()
    link, total = None, None
    refreshes = idle = fails = 0
    started = time.time()
    bytes_this_run = 0
    last_print = 0.0

    def expired(why):
        nonlocal link, refreshes, idle
        idle += 1
        if idle > max_idle_refresh:
            raise DownloadError(f"fresh links keep failing without progress ({why})")
        log(f"\nlink no longer works ({why}); getting a fresh one")
        link, refreshes = None, refreshes + 1
        sleep(1)

    def failed(why):
        nonlocal link, fails, refreshes
        fails += 1
        log(f"\nproblem: {why}; waiting then retrying ({fails})")
        sleep(min(2 ** fails, 30))
        if fails >= 4:
            link, refreshes, fails = None, refreshes + 1, 0

    def finish():
        os.replace(part, dest)
        if progress:
            print(file=sys.stderr)
        return dest

    while True:
        if link is None:
            link = get_link(refreshes > 0)
        offset = _size(part)
        headers = {"User-Agent": UA, "Accept": "*/*", "Accept-Encoding": "identity"}
        if link.referer:
            headers["Referer"] = link.referer
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            resp = opener.open(Request(safe_url(link.url), headers=headers), timeout=stall)
        except HTTPError as e:
            if e.code == 416:
                cr = e.headers.get("Content-Range") or ""
                if offset and cr.endswith("/" + str(offset)):
                    return finish()            # the partial file is already complete
                log("\npartial file does not fit the server's file; starting over")
                os.remove(part)
            elif e.code in EXPIRED:
                expired(f"HTTP {e.code}")
            else:
                failed(f"HTTP {e.code}")
            continue
        except (URLError, OSError, HTTPException) as e:
            failed(str(e))
            continue

        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            resp.close()
            expired("got a web page instead of the file")
            continue

        cr = resp.headers.get("Content-Range")
        clen = resp.headers.get("Content-Length")
        if resp.status == 206:
            mode = "ab"
            if cr and "/" in cr and not cr.rstrip().endswith("/*"):
                total = int(cr.rsplit("/", 1)[1])
            elif clen:
                total = offset + int(clen)
        else:
            if offset:
                log("\nserver ignored the resume request; starting over")
                offset = 0
            mode = "wb"
            total = int(clen) if clen else None

        got_now, dropped = 0, False
        try:
            with open(part, mode) as f:
                while True:
                    chunk = resp.read(CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    got_now += len(chunk)
                    bytes_this_run += len(chunk)
                    now = time.time()
                    if progress and now - last_print >= 1.0:
                        last_print = now
                        done = offset + got_now
                        speed = bytes_this_run / max(now - started, 0.001)
                        pct = f"{100 * done / total:5.1f}%" if total else "   ?  "
                        left = (total - done) / speed if total and speed else None
                        sys.stderr.write(f"\r{pct} {human(done)}/{human(total) if total else '?'}"
                                         f"  {human(speed)}/s  ETA {eta(left)}   ")
                        sys.stderr.flush()
        except (OSError, HTTPException) as e:
            dropped = True
            log(f"\nconnection dropped ({type(e).__name__}); continuing from the saved part")
        finally:
            resp.close()

        have = _size(part)
        if got_now:
            fails = idle = 0
        if total is not None:
            if have == total:
                return finish()
            if have > total:
                log("\nfile is larger than expected; starting over")
                os.remove(part)
                continue
            if not got_now and not dropped:
                failed("empty reply")
        elif not dropped and got_now:
            return finish()
        elif not got_now:
            failed("empty reply")
