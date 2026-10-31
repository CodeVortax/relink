# relink

A downloader for the sites that don't give you a real file link.

## The problem

When you download a movie or an episode from a site, you often don't get a
permanent link. You get a **temporary one** that expires after a few minutes.
If your connection drops halfway, the link is dead and the download is lost.
Starting over is painful, especially on mobile data.

## The idea

A download should never be tied to one link. relink remembers the **page**
and **how to get the link from it**. When the link expires or the connection
drops, it fetches a fresh link and **continues from where it stopped**.

## Goals

- **Survive bad connections.** Retry, refresh the link, resume. No manual restarts.
- **Continuous link refresh.** Expired or tokenized links get replaced automatically.
- **Generic mode.** Works like a normal video downloader for any site it already understands.
- **Template mode.** Per-site recipes for popular sites that generic tools handle badly.
- **Easy to extend.** Adding a site means writing a small template file, not new code.
- **Runs on a phone.** Built for Termux first.

## How it works

1. A **template** (`templates/<site>.yml`) says how to get from a page to a direct file link:
   read the page's server list, open the download host, pick a quality, submit the form.
   Templates are small step lists (`get`, `post`, `form`, `find`, `choose`), see `relink/engine.py`.
2. The downloader saves to `name.mp4.part` and resumes with HTTP Range.
3. If the link dies (403/410, a web page instead of the file, a dropped connection) it runs
   the template again for a fresh link and continues from the same byte.
4. Job state is saved in `~/.relink/jobs`, so `relink resume` continues after a reboot.

Templates need only Python and PyYAML. yt-dlp is optional (generic mode).

## Install (Termux)

```bash
pkg install -y python git
git clone https://github.com/CodeVortax/relink
cd relink
pip install -e .          # installs pyyaml and the `relink` command
```

## Usage

```bash
relink search the mentalist               # small search API (episodes)
relink servers <episode-url>              # servers on a page and which have an adapter
relink get <episode-url>                  # best quality, saved to ~/storage/downloads/relink
relink get <url> --quality low --out ~/storage/downloads
relink get <url> --dry                    # only print the direct link
relink list                               # jobs and progress
relink resume                             # continue unfinished jobs
```

If a site shows a Cloudflare challenge, pass the browser's cookie: `--cookie "cf_clearance=..."`.

## Generic mode (yt-dlp fallback)

If no template matches a URL, relink hands it to yt-dlp, with settings for a bad connection
(continue `.part` files, retry forever). Same commands, same `relink list` / `resume`:

```bash
pip install yt-dlp && pkg install -y ffmpeg      # once
relink get https://www.youtube.com/watch?v=...   # no template: uses yt-dlp
relink get <url> --ytdlp                         # force yt-dlp even if a template matches
```

## Web search API: `relink web`

One call over Google (basic no-JS page), DuckDuckGo, Bing, Brave and Mojeek. No API keys.
Results are merged (a page several engines return ranks higher) and the report says which
engine failed and why (captcha, timeout, page changed), so one blocked engine never hides the rest.

```bash
relink web the mentalist s01e01                      # google + ddg + bing
relink web -e google -e mojeek --lang ar --region eg مسلسل ذا مينتاليست
relink web --pages 2 --limit 20 --json some query   # JSON for scripts
relink web --serve --port 8765                       # local API: curl 'localhost:8765/search?q=...&engines=google,ddg'
```

From Python: `from relink.search_api import search; search("query", ["google", "bing"], lang="ar")`.
Engines change their pages and block scripts now and then: each has its own parser plus a
generic fallback, but expect Google in particular to refuse some networks.

## Finding more sites: `relink discover`

Search for sites, study each one, group the clones, and pack a small bundle to send back
so templates can be written without browsing by hand:

```bash
relink discover --title "The Mentalist" --title "Breaking Bad"   # 5 queries per title (EN + AR)
relink discover "مشاهدة مسلسلات مترجمة اون لاين"                   # or your own words
relink discover --site https://some-site.tv --urls sites.txt     # skip search, study these
```

It searches with the web API above (Google, DuckDuckGo, Bing; `--engine` picks), fingerprints every site
(CMS and theme, search form, watch/download links, embed hosts, Cloudflare) and groups sites
with the same theme into a **family**: one template usually covers the whole family.
For the best sites it saves trimmed sample pages (home, search, watch, download) into
`relink-discover/relink-discover.tar.gz` (usually well under 100 KB) with `report.md`.
Results are saved after each site, so re-running continues where it stopped.
If the search engines refuse your network, pass sites with `--site` / `--urls`.

## Studying a new site

`tools/probe.py` fetches one page and prints its structure (iframes, forms with hidden
fields, scripts, links, media URLs). Useful options: `--html FILE --show REGEX` (offline),
`--data "a=b"` (POST), `--submit-form` (post the first form and show the reply).
Then write the steps in `templates/<site>.yml`. `templates/mycima.yml` is the worked example.

## Status

- [x] Template engine, resumable downloader with automatic link refresh, job state
- [x] First template: MyCima, episode page to direct link (vipserver host, HD and low)
- [x] Offline test with a fake site (cut connection, expired link): `python -m unittest discover -s tests`
- [ ] Prove the chain on the real site (needs a run on a connection that reaches it)
- [ ] Series mode: all episodes of a season, next-episode following
- [x] Generic mode: yt-dlp fallback when no template matches
- [x] `relink discover`: search, fingerprint, group into families, bundle samples
- [ ] Adapters for the other hosts (mixdrop, abstream, minochinos, hgcloud, bysebuho)
- [ ] Site analyzer that drafts a template from a family's bundle

## Notes

Only download content you have the right to download, and respect each site's terms.
