"""Generic mode: hand a URL to yt-dlp when no template matches.

relink's own templates cover the sites yt-dlp handles badly. For everything else
(YouTube, Vimeo, TikTok, ...) yt-dlp already knows what to do, so we just run it
with settings that survive a bad connection: it continues `.part` files and retries
without limit. Needs `pip install yt-dlp`, plus `pkg install ffmpeg` to merge streams.
"""
import importlib.util
import os
import shutil
import subprocess
import sys

from .engine import ResolveError

# relink quality names -> yt-dlp format selectors
FORMATS = {
    "best": "bv*+ba/b",
    "hd": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "low": "bv*[height<=480]+ba/b[height<=480]/w",
}


def ytdlp_command():
    """The command that runs yt-dlp, or None. RELINK_YTDLP overrides it (used by tests)."""
    env = os.environ.get("RELINK_YTDLP")
    if env:
        return env.split()
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    return None


def need_ytdlp():
    cmd = ytdlp_command()
    if not cmd:
        raise ResolveError(
            "no template matches this URL and yt-dlp is not installed.\n"
            "  generic mode:  pip install yt-dlp   (and: pkg install ffmpeg)\n"
            "  or write a template for the site, see tools/probe.py")
    return cmd


def build_args(url, quality, outdir, dry=False):
    """Arguments after the yt-dlp command itself."""
    if dry:
        return ["-g", "-f", FORMATS.get(quality, FORMATS["best"]), "--no-playlist", url]
    return [
        "-f", FORMATS.get(quality, FORMATS["best"]),
        "-c",                                    # continue .part files
        "--no-playlist",
        "--retries", "infinite",
        "--fragment-retries", "infinite",
        "--socket-timeout", "30",
        "--merge-output-format", "mp4",
        "--newline",
        "-o", os.path.join(str(outdir), "%(title).150B [%(id)s].%(ext)s"),
        url,
    ]


def run(url, quality, outdir, dry=False):
    """Run yt-dlp, output goes straight to the terminal. Returns its exit code."""
    cmd = need_ytdlp() + build_args(url, quality, outdir, dry)
    try:
        return subprocess.call(cmd)
    except KeyboardInterrupt:
        return 130
