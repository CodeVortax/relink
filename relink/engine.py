"""Template engine: runs the small step language written in a site template.

A template (templates/<site>.yml) says how to get from a page URL to a direct file link:
  match    regex that recognises the site's URLs (named groups become variables)
  servers  steps that read the episode page and produce `servers` (name + embed URL)
  hosts    per download host: steps that turn one server into a direct link, plus `result`
  search   steps that turn a query into `results`

Steps (each list item has exactly one key):
  get: URL | {url, referer}          fetch a page      -> page, page_url
  post: {url, form: {...}, ajax}     POST a form       -> page, page_url
  form: {ajax}                       POST the first form with hidden fields found in `page`
  find: {regex, from, all, dotall, strip, as, required}
                                     regex over `page` (or variable `from`); named groups
                                     become variables, `all` makes a list of dicts under `as`
  choose: {from, key, prefer, as}    pick one item of a list by quality preference
  set: {name: "text {var}"}          make variables

Text values can use {variable} and {a.b}. In regexes the values are escaped.
"""
import re
from html import unescape
from pathlib import Path
from urllib.parse import unquote, urlencode, urljoin, urlsplit

try:
    import yaml
except ImportError:  # reported when templates are loaded
    yaml = None

from .net import NetError

VAR = re.compile(r"\{([A-Za-z_][\w.]*)\}")


class TemplateError(Exception):
    pass


class ResolveError(Exception):
    pass


def lookup(ctx, dotted):
    cur = ctx
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise TemplateError(f"unknown variable {{{dotted}}}")
    return cur


def fmt(text, ctx, escape=False):
    def sub(m):
        value = str(lookup(ctx, m.group(1)))
        return re.escape(value) if escape else value
    return VAR.sub(sub, text)


def strip_tags(text):
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def clean_filename(name):
    name = re.sub(r"^\[[^\]]*\]", "", name)          # leading [site tag]
    name = re.sub(r'[\x00-\x1f/\\:*?"<>|]', "_", name).strip(" .")
    return name


def origin_of(url):
    u = urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


def hidden_form(html, base):
    """First form that has hidden inputs -> (action_url, [(name, value), ...])."""
    for m in re.finditer(r"(<form\b[^>]*>)(.*?)(?:</form>|\Z)", html, re.I | re.S):
        inputs = re.findall(r"<input\b[^>]*>", m.group(2), re.I)
        if not any(re.search(r"type=[\"']hidden", t, re.I) for t in inputs):
            continue
        fields = []
        for tag in inputs:
            n = re.search(r"\bname=[\"']([^\"']*)", tag, re.I)
            v = re.search(r"\bvalue=[\"']([^\"']*)", tag, re.I)
            if n:
                fields.append((n.group(1), unescape(v.group(1)) if v else ""))
        act = re.search(r"\baction=[\"']([^\"']*)", m.group(1), re.I)
        action = unescape(act.group(1)) if act and act.group(1) else base
        return urljoin(base, action), fields
    return None, None


class Template:
    def __init__(self, data, path=None):
        self.d, self.path = data, path
        try:
            self.name = data["name"]
            self.match = re.compile(data["match"])
        except KeyError as e:
            raise TemplateError(f"{path}: missing {e}")

    def hosts(self):
        hosts = self.d.get("hosts") or {}
        return {k.lower(): v for k, v in hosts.items() if isinstance(v, dict) and v.get("steps")}


def template_dirs():
    import os
    dirs = []
    if os.environ.get("RELINK_HOME"):
        dirs.append(Path(os.environ["RELINK_HOME"]) / "templates")
    dirs.append(Path.home() / ".relink" / "templates")
    dirs.append(Path(__file__).resolve().parent.parent / "templates")
    dirs.append(Path("templates"))
    return dirs


def load_templates(dirs=None):
    if yaml is None:
        raise TemplateError("PyYAML is missing. Install it with:  pip install pyyaml")
    found, seen = [], set()
    for d in dirs or template_dirs():
        for path in sorted(Path(d).glob("*.yml")) if Path(d).is_dir() else []:
            if path.resolve() in seen:
                continue
            seen.add(path.resolve())
            try:
                found.append(Template(yaml.safe_load(path.read_text(encoding="utf-8")), path))
            except Exception as e:
                raise TemplateError(f"{path}: {e}")
    return found


def find_template(url, templates):
    for t in templates:
        if t.match.search(url):
            return t
    return None


class Resolved:
    def __init__(self, url, filename, server, label, referer, title, refresh):
        self.url, self.filename, self.server = url, filename, server
        self.label, self.referer, self.title = label, referer, title
        self.refresh = refresh  # () -> Resolved with a fresh link, same server and quality


class Engine:
    def __init__(self, client, log=lambda m: None):
        self.client, self.log = client, log

    # ---- step runner ---------------------------------------------------------------
    def run(self, steps, ctx):
        for step in steps or []:
            if not isinstance(step, dict) or len(step) != 1:
                raise TemplateError(f"each step needs exactly one key: {step!r}")
            (op, arg), = step.items()
            fn = getattr(self, "op_" + op, None)
            if not fn:
                raise TemplateError(f"unknown step '{op}'")
            fn(arg if arg is not None else {}, ctx)

    def _load(self, resp, ctx, url):
        if resp.status >= 400:
            raise ResolveError(f"HTTP {resp.status} for {url}")
        ctx["page"], ctx["page_url"] = resp.text, resp.url

    def op_get(self, arg, ctx):
        if isinstance(arg, str):
            arg = {"url": arg}
        url = fmt(arg["url"], ctx)
        referer = fmt(arg["referer"], ctx) if arg.get("referer") else None
        self._load(self.client.request(url, referer=referer), ctx, url)

    def op_post(self, arg, ctx):
        url = fmt(arg["url"], ctx)
        form = {k: fmt(str(v), ctx) for k, v in (arg.get("form") or {}).items()}
        referer = fmt(arg["referer"], ctx) if arg.get("referer") else None
        resp = self.client.request(url, data=urlencode(form), referer=referer,
                                   ajax=bool(arg.get("ajax")))
        self._load(resp, ctx, url)

    def op_form(self, arg, ctx):
        page_url = ctx.get("page_url")
        action, fields = hidden_form(ctx.get("page", ""), page_url or "")
        if not action:
            raise ResolveError("expected a form with hidden fields on the page, found none")
        resp = self.client.request(action, data=urlencode(fields), referer=page_url,
                                   ajax=bool(arg.get("ajax")))
        self._load(resp, ctx, action)

    def op_find(self, arg, ctx):
        if arg.get("unless") and ctx.get(arg["unless"]):
            return                      # a variable we wanted is already set, skip this fallback
        src = ctx.get(arg.get("from") or "page")
        if not isinstance(src, str):
            raise ResolveError(f"find: no text in '{arg.get('from') or 'page'}'")
        pattern = fmt(arg["regex"], ctx, escape=True)
        flags = re.I | (re.S if arg.get("dotall") else 0)
        strip = set(arg.get("strip") or [])

        def values(m):
            out = {}
            for k, v in m.groupdict().items():
                v = v or ""
                out[k] = strip_tags(v) if k in strip else unescape(v)
            return out

        required = arg.get("required", True)
        if arg.get("all"):
            rows = [values(m) for m in re.finditer(pattern, src, flags)]
            if not rows and required:
                raise ResolveError(f"find: nothing matched {pattern[:70]}")
            ctx[arg.get("as", "found")] = rows
            return
        m = re.search(pattern, src, flags)
        if not m:
            if required:
                raise ResolveError(f"find: nothing matched {pattern[:70]}")
            return
        row = values(m)
        ctx.update(row)
        if arg.get("as"):
            ctx[arg["as"]] = row

    def op_choose(self, arg, ctx):
        items = ctx.get(arg["from"]) or []
        if not items:
            raise ResolveError(f"choose: '{arg['from']}' is empty")
        key, want = arg["key"], ctx.get("quality") or "best"
        prefer = arg.get("prefer") or {}
        order = prefer.get(want, [want])
        for wanted in order:
            for item in items:
                if item.get(key) == wanted:
                    ctx[arg.get("as", "pick")] = item
                    return
        self.log(f"  '{want}' not offered; using {items[0].get('label') or items[0]}")
        ctx[arg.get("as", "pick")] = items[0]

    def op_set(self, arg, ctx):
        for k, v in arg.items():
            ctx[k] = fmt(str(v), ctx)

    # ---- high level ----------------------------------------------------------------
    def base_ctx(self, tpl, url):
        m = tpl.match.search(url)
        if not m:
            raise ResolveError(f"{url} does not match template '{tpl.name}'")
        return {"url": url, "origin": origin_of(url), **m.groupdict()}

    def servers(self, tpl, url):
        ctx = self.base_ctx(tpl, url)
        self.run((tpl.d.get("servers") or {}).get("steps"), ctx)
        return ctx, ctx.get("servers") or []

    def resolve(self, tpl, url, server=None, quality="best"):
        ctx, servers = self.servers(tpl, url)
        if not servers:
            raise ResolveError("no servers found on the page")
        hosts = tpl.hosts()
        priority = [n.lower() for n in tpl.d.get("server_priority") or []]
        usable = [s for s in servers if s["name"].lower() in hosts]
        if server:
            usable = [s for s in usable if s["name"].lower() == server.lower()]
        usable.sort(key=lambda s: priority.index(s["name"].lower())
                    if s["name"].lower() in priority else len(priority))
        if not usable:
            have = ", ".join(s["name"] for s in servers)
            raise ResolveError(f"no supported server. The page has: {have}")
        base_keys = {k: v for k, v in ctx.items() if k not in ("page", "page_url", "servers")}
        last = None
        for s in usable:
            base = dict(base_keys)
            base.update(s)
            base["quality"] = quality
            base["embed_origin"] = origin_of(s["embed"])
            try:
                return self._host(hosts[s["name"].lower()], base, s["name"])
            except (ResolveError, NetError) as e:
                last = e
                self.log(f"  {s['name']} failed: {e}")
        raise ResolveError(f"no server worked (last error: {last})")

    def _host(self, host, base, name):
        ctx = dict(base)
        self.run(host["steps"], ctx)
        direct = fmt(host["result"], ctx)
        filename = clean_filename(unquote(urlsplit(direct).path.rsplit("/", 1)[-1]))
        filename = filename or f"{ctx.get('vid', 'video')}.mp4"
        pick = ctx.get("pick") or {}
        return Resolved(direct, filename, name, pick.get("label", ""), ctx.get("page_url"),
                        strip_tags(ctx.get("title", "")),
                        lambda: self._host(host, base, name))

    def search(self, tpl, query, origin=None):
        s = tpl.d.get("search")
        if not s:
            raise ResolveError(f"template '{tpl.name}' has no search")
        if not origin:
            root = (tpl.d.get("site") or {}).get("root")
            if not root:
                raise ResolveError("no site root in the template; pass --base")
            origin = origin_of(self.client.request(root).url)
        ctx = {"origin": origin, "query": query}
        self.run(s["steps"], ctx)
        rows = ctx.get("results") or []
        link = s.get("result_url")
        for r in rows:
            r["url"] = fmt(link, {**ctx, **r}) if link else ""
        return rows
