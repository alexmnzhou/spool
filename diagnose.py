"""Check whether a URL's formats can actually be range-seeked, i.e. whether clipping
a few minutes out of a long video will be fast.

    python diagnose.py "https://youtube.com/watch?v=..."
    python diagnose.py "https://..." --seek-test     # also times a real deep seek

Reports, per selected stream:
  * protocol / fragmentation  - decides which downloader yt-dlp picks
  * whether FFmpegFD accepts it - if not, clipping aborts outright
  * whether the CDN honours HTTP Range - if not, ffmpeg reads from byte 0
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yt_dlp
from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.downloader.external import FFmpegFD
from yt_dlp.networking import Request

COOKIES = Path(__file__).parent / "cookies.txt"
DEEP_SEEK = 3600.0      # 1h in - far enough that a sequential read would be obvious


def build_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestvideo+bestaudio/best",
        "js_runtimes": {"node": {}},
        "remote_components": {"ejs:github"},
    }
    if COOKIES.exists():
        opts["cookiefile"] = str(COOKIES)
    return opts


def check_range(ydl, fmt) -> str:
    """Ask the CDN for 100 bytes from deep in the file and see if it obliges."""
    # Stay inside the file - an offset past EOF answers 416 and tells us nothing.
    size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
    offset = min(5_000_000, size // 2) if size else 100_000
    headers = {**(fmt.get("http_headers") or {}), "Range": f"bytes={offset}-{offset + 99}"}
    try:
        resp = ydl.urlopen(Request(fmt["url"], headers=headers))
    except Exception as e:
        return f"probe failed ({type(e).__name__}: {e})"

    status = resp.status
    content_range = resp.headers.get("Content-Range")
    resp.close()

    if status == 206 and content_range:
        return f"YES - 206 Partial Content, {content_range}"
    if status == 200:
        return "NO - server returned 200 and ignored Range (ffmpeg would read from byte 0)"
    return f"unclear - HTTP {status}, Content-Range={content_range}"


def seek_test(fmt):
    """Time a shallow vs deep seek. Similar timings => real seeking, not sequential."""
    header_args = []
    if fmt.get("http_headers"):
        header_args = ["-headers", "".join(f"{k}: {v}\r\n" for k, v in fmt["http_headers"].items())]

    results = {}
    for label, offset in (("shallow (5s in)", 5.0), (f"deep ({DEEP_SEEK / 3600:g}h in)", DEEP_SEEK)):
        out = Path(tempfile.gettempdir()) / f"seektest_{int(offset)}.mp4"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               *header_args, "-ss", str(offset), "-i", fmt["url"],
               "-t", "3", "-c", "copy", str(out)]
        started = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        elapsed = time.time() - started
        size = out.stat().st_size if out.exists() else 0
        results[label] = (elapsed, size, proc.returncode, proc.stderr.strip()[:200])
        out.unlink(missing_ok=True)
    return results


def main(url, do_seek_test):
    opts = build_opts()
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

        dur = info.get("duration")
        print(f"\n{info.get('title', '?')}")
        print(f"duration: {dur / 3600:.2f}h ({dur}s)\n" if dur else "duration: unknown\n")

        # Mirror what a clip download would look like to the downloader picker.
        probe_info = dict(info, section_start=DEEP_SEEK, section_end=DEEP_SEEK + 30)
        fd = get_suitable_downloader(probe_info, ydl.params)
        print(f"downloader yt-dlp would use for a clip: {fd.__name__}")
        if fd is not FFmpegFD:
            print("  ^^ NOT FFmpegFD - yt-dlp will abort with")
            print("     'This format cannot be partially downloaded. Aborting'")
        print()

        streams = info.get("requested_formats") or [info]
        for fmt in streams:
            frags = fmt.get("fragments")
            print(f"[{fmt.get('format_id')}] {fmt.get('ext')} "
                  f"{fmt.get('vcodec', '?')}/{fmt.get('acodec', '?')}")
            print(f"  protocol:   {fmt.get('protocol')}")
            print(f"  fragmented: {'yes - ' + str(len(frags)) + ' fragments' if frags else 'no (single file)'}")
            print(f"  range seek: {check_range(ydl, fmt)}")
            print()

        if do_seek_test:
            if not dur or dur < DEEP_SEEK + 60:
                print(f"skipping seek test - video shorter than {DEEP_SEEK}s")
                return
            print(f"seek test (downloading 3s at each offset)…")
            for fmt in streams[:1]:
                for label, (elapsed, size, rc, err) in seek_test(fmt).items():
                    status = "ok" if rc == 0 else f"FAILED rc={rc} {err}"
                    print(f"  {label:<18} {elapsed:6.1f}s  {size / 1024:8.0f} KB  {status}")
            print("\n  Similar timings => ffmpeg is seeking properly.")
            print("  Deep seek much slower => sequential read; clipping deep into a")
            print("  long video will be slow no matter what the GUI does.")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)
    main(args[0], "--seek-test" in sys.argv)
