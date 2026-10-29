"""A local fake of the MyCima + vipserver chain, so the engine can be tested offline.

Pages mirror what tools/probe.py saw on the real site (same ids, forms, labels).
The file server misbehaves on purpose: it cuts the connection halfway, then expires
the signed link, so the downloader has to fetch a fresh link and resume.
"""
import hashlib
import itertools
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CODE = "epbo39gk2fsb"
VID = "bb54280c0"
TITLE = "مسلسل The Mentalist الموسم الثالث الحلقة 17 مترجم ماي سيما"
FILE = hashlib.sha256(b"relink").digest() * 9000          # 288000 bytes, easy to verify
NAME = "[Cimawbas]The.Mentalist.S03.E17.mp4"


class State:
    def __init__(self, mode="cut"):
        self.mode = mode                  # cut | html | clean
        self.counter = itertools.count(1)
        self.tokens = {}                  # token -> {"uses": n, "dead": bool}
        self.forms_posted = 0
        self.file_requests = []
        self.lock = threading.Lock()


def make_handler(state, port):
    base = f"http://127.0.0.1:{port}"

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        # ---------------------------------------------------------------- GET
        def do_GET(self):
            u = urlsplit(self.path)
            q = parse_qs(u.query)
            if u.path == "/play.php" and q.get("vid") == [VID]:
                servers = [
                    ("Vipserver", f"{base}/embed-{CODE}.html"),
                    ("Vkvideo", "https://vkvideo.ru/video_ext.php?oid=1&id=2&hash=&hd=1"),
                    ("Mixdrop", "https://mixdrop.top/e/k0wn9rqmh3o8qp0"),
                ]
                lis = "".join(
                    f'<li class="" id="server_{n}" data-embed="<iframe src=\'{e}\' '
                    f'allow=\'encrypted-media *;\' frameborder=\'0\' allowfullscreen=\'\'></iframe>">'
                    f"<span>x</span> {n}</li>" for n, e in servers)
                return self.send(200, f"<html><head><title>{TITLE}</title></head>"
                                      f"<body><ul class='list_embedded'>{lis}</ul></body></html>")
            if u.path == f"/d/{CODE}.html":
                return self.send(200, f"""<html><body><a href="{base}/">x</a>
                  <a href="{base}/d/{CODE}_h">HD quality 1280x720, 363.7 MB</a>
                  <a href="{base}/d/{CODE}_l">Low quality 640x360, 124.6 MB</a></body></html>""")
            m = re.fullmatch(rf"/d/{CODE}_(\w)", u.path)
            if m:
                with state.lock:
                    n = next(state.counter)
                return self.send(200, f"""<html><body>
                  <Form method="POST" id="F1" action="">
                  <input type="hidden" name="op" value="download_orig">
                  <input type="hidden" name="id" value="{CODE}">
                  <input type="hidden" name="mode" value="{m.group(1)}">
                  <input type="hidden" name="hash" value="603464-5-181-{n}-aa">
                  <button>Create download link</button></Form></body></html>""",
                                 extra={"Set-Cookie": "lang=1; path=/"})
            if u.path.startswith("/v/"):
                return self.file(q)
            self.send(404, "not found")

        # --------------------------------------------------------------- POST
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            form = parse_qs(self.rfile.read(length).decode())
            u = urlsplit(self.path)
            if u.path == "/ajax-search.php":
                q = form.get("queryString", [""])[0].lower()
                if "mentalist" not in q:
                    return self.send(200, "")
                items = "".join(
                    f"<li onClick=\"fill('{TITLE} {i}');\" data-video-id=\"id{i:07d}\">"
                    f"<a href=\"{base}/watch.php?vid=id{i:07d}\">{TITLE} {i}</a></li>"
                    for i in (13, 12, 11))
                return self.send(200, f"<ul>{items}</ul>")
            m = re.fullmatch(rf"/d/{CODE}_(\w)", u.path)
            if m and form.get("op") == ["download_orig"] and form.get("hash", [""])[0].startswith("603464"):
                with state.lock:
                    state.forms_posted += 1
                    token = f"tok{state.forms_posted}"
                    state.tokens[token] = {"uses": 0, "dead": False}
                link = f"{base}/v/01/00120/{CODE}_{m.group(1)}/{NAME}?t={token}&s=1&e=43200"
                return self.send(200, f'<html><body><a class="btn" href="{link.replace("&", "&amp;")}">'
                                      f'<span>x</span> Direct Download Link</a></body></html>')
            self.send(400, "bad form")

        # ---------------------------------------------------------- file server
        def file(self, q):
            token = (q.get("t") or [""])[0]
            with state.lock:
                t = state.tokens.get(token)
                state.file_requests.append((token, self.headers.get("Range")))
            if not t or t["dead"]:
                if state.mode == "html":
                    return self.send(200, "<html>link expired</html>")
                return self.send(403, "expired")
            start = 0
            rng = self.headers.get("Range")
            if rng:
                start = int(re.match(r"bytes=(\d+)-", rng).group(1))
                if start >= len(FILE):
                    return self.send(416, "", extra={"Content-Range": f"bytes */{len(FILE)}"})
            body = FILE[start:]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Length", str(len(body)))
            if rng:
                self.send_header("Content-Range", f"bytes {start}-{len(FILE) - 1}/{len(FILE)}")
            self.end_headers()
            with state.lock:
                t["uses"] += 1
                cut = state.mode in ("cut", "html") and t["uses"] == 1 and token == "tok1"
                if cut:
                    t["dead"] = True          # the link dies right after the connection drops
            if cut:
                self.wfile.write(body[: len(body) // 3])
                self.wfile.flush()
                self.close_connection = True
                return                         # connection ends early: Content-Length not reached
            self.wfile.write(body)


    return H


def start(mode="cut"):
    state = State(mode)
    server = ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: None)
    port = server.server_address[1]
    server.RequestHandlerClass = make_handler(state, port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, port
