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

No yt-dlp or aria2 needed so far: only Python and PyYAML.

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
- [ ] Adapters for the other hosts (mixdrop, abstream, minochinos, hgcloud, bysebuho)
- [ ] Site analyzer that drafts a template from a link

## Notes

Only download content you have the right to download, and respect each site's terms.
