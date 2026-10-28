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

1. `resolve`: match the URL to a template (or fall back to yt-dlp) and get a direct link.
2. `download`: aria2c fetches it with resume and multiple connections.
3. If the link dies, go back to step 1 and resume the partial file.
4. Job state is saved, so `relink resume` continues everything after a reboot.

## Tools used

- [yt-dlp](https://github.com/yt-dlp/yt-dlp): generic fallback for many sites
- [aria2](https://aria2.github.io/): resumable, multi-connection downloads
- ffmpeg: merging streams
- Python

## Install (Termux)

```bash
pkg install -y python git aria2 ffmpeg
git clone https://github.com/CodeVortax/relink
cd relink
pip install -e .
```

## Usage

```bash
relink get <url>      # download a page or direct link
relink resume         # continue unfinished downloads
relink list           # show all jobs
```

## Templates (coming next)

Each supported site gets one small file in `templates/`:

```yaml
name: example-site
match: "https?://(www\\.)?example\\.com/watch/.+"
steps:
  - fetch: "{url}"
  - extract: {regex: 'data-src="(.*?)"', as: player}
  - fetch: "{player}"
  - extract: {json: "sources.0.file", as: media}
headers: {Referer: "{url}"}
result: "{media}"
```

## Roadmap

- [x] Project skeleton, yt-dlp fallback, aria2 resume
- [ ] Template engine
- [ ] Expiry watchdog (detect 403/410 and stalls)
- [ ] Queue and batch mode
- [ ] First templates for popular sites

## Notes

Only download content you have the right to download, and respect each site's terms.
