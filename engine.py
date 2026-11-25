"""Spool's download engine: everything except the window.

Events go out through a single `emit(name, payload)` callback, so the engine has no
idea what's drawing it and never touches UI state from its worker threads:

    log       {"msg": str}
    status    {"text": str}
    progress  {"fraction": float | None, "speed": str, "eta": str, "detail": str}
              (fraction None = working, amount unknown)
    finished  {"state": "done" | "error" | "cancelled", "message": str, "path": str | None}
    frames    {"path": str, "board": dict}   (timeline frames for a local file, sent
              after its preview because grabbing them takes a few seconds)
"""
import os
import subprocess
import threading
import time
from pathlib import Path

import yt_dlp
import yt_dlp.utils

import clipper
import localfile

FORMATS = {
    "Best Quality":     {"format": "bestvideo+bestaudio/best"},
    "1080p":            {"format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best"},
    "720p":             {"format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best"},
    "480p":             {"format": "bestvideo[height<=480]+bestaudio/best[height<=480]/best"},
    "Audio Only (MP3)": {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
    },
}

BROWSERS = ["chrome", "firefox", "edge", "chromium", "brave", "opera", "safari", "vivaldi"]

# Formats with no video stream — merge_output_format is meaningless for these.
AUDIO_ONLY_FORMATS = {"Audio Only (MP3)"}

# Clipping reads the MP4 `sidx` index directly (see clipper.py), so both streams
# have to be plain-HTTPS DASH MP4/M4A. WebM/Opus indexes segments differently, and
# YouTube's HLS formats also report ext=mp4 but are m3u8 playlists with no sidx.
_V, _A = "[ext=mp4][protocol=https]", "[ext=m4a][protocol=https]"
CLIP_FORMATS = {
    "Best Quality":     f"bestvideo{_V}+bestaudio{_A}/best[ext=mp4]/best",
    "1080p":            f"bestvideo{_V}[height<=1080]+bestaudio{_A}/best[ext=mp4][height<=1080]/best",
    "720p":             f"bestvideo{_V}[height<=720]+bestaudio{_A}/best[ext=mp4][height<=720]/best",
    "480p":             f"bestvideo{_V}[height<=480]+bestaudio{_A}/best[ext=mp4][height<=480]/best",
    "Audio Only (MP3)": f"bestaudio{_A}/bestaudio/best",
}

# HLS-first selectors. Used (a) for clips on the yt-dlp/ffmpeg path, since HLS is a
# playlist of small segments ffmpeg seeks through happily, whereas it stalls forever
# on YouTube's single-file DASH streams; and (b) as the last retry when YouTube 403s
# the direct DASH URLs, which it now does intermittently. Each entry falls back to
# the normal selector for sites that don't offer HLS.
_H = "[protocol^=m3u8]"
HLS_FORMATS = {
    "Best Quality":     f"bestvideo{_H}+bestaudio{_H}/best{_H}/" + FORMATS["Best Quality"]["format"],
    "1080p":            f"bestvideo{_H}[height<=1080]+bestaudio{_H}/best{_H}[height<=1080]/" + FORMATS["1080p"]["format"],
    "720p":             f"bestvideo{_H}[height<=720]+bestaudio{_H}/best{_H}[height<=720]/" + FORMATS["720p"]["format"],
    "480p":             f"bestvideo{_H}[height<=480]+bestaudio{_H}/best{_H}[height<=480]/" + FORMATS["480p"]["format"],
    "Audio Only (MP3)": f"bestaudio{_H}/" + FORMATS["Audio Only (MP3)"]["format"],
}

STALL_TIMEOUT = 120     # seconds of zero ffmpeg output before we call it hung

# Cookies.txt in the same folder as this script, if present
DEFAULT_COOKIES_FILE = Path(__file__).parent / "cookies.txt"


def parse_time(s: str) -> float | None:
    """Parse HH:MM:SS, MM:SS, or plain seconds into a float. Returns None if blank or invalid."""
    s = (s or "").strip()
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    return None  # invalid


# ffmpeg reports an HTTP 403 only as its exit code: AVERROR_HTTP_FORBIDDEN is
# -MKTAG(0xF8,'4','0','3') = -858797304, which Windows shows as 3436169992.
_FFMPEG_403_CODES = ("3436169992", "-858797304")


def _is_forbidden(exc: Exception) -> bool:
    """True for an HTTP 403 from yt-dlp's downloader, our ranged fetch, or ffmpeg."""
    msg = str(exc)
    return ("HTTP Error 403" in msg
            or getattr(exc, "status", None) == 403
            or ("ffmpeg exited with code" in msg and any(c in msg for c in _FFMPEG_403_CODES)))


def _fmt_bytes(n) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def clip_suffix(start, end) -> str:
    """' (clip 10m00s-10m10s)' — keeps clips from colliding with the full video,
    which yt-dlp would otherwise treat as 'already downloaded' and silently skip."""
    def part(t):
        if t is None:
            return "end"
        t = int(t)
        h, rem = divmod(t, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m}m{sec:02d}s"
    return f" (clip {part(start or 0)}-{part(end)})"


def _fmt_eta(seconds) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class _Logger:
    """Forwards yt-dlp log messages to the engine's log event."""
    def __init__(self, log_fn):
        self._log = log_fn

    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        self._log(msg)

    def info(self, msg):
        self._log(msg)

    def warning(self, msg):
        self._log(f"Warning: {msg}")
        if "no longer valid" in msg and "cookies" in msg.lower():
            self._log(
                "Hint: your cookies.txt is expired — YouTube rotated it.\n"
                "Re-export from Chrome (F12 → Application → Cookies) or use\n"
                "the 'Browser' cookie source instead."
            )

    def error(self, msg):
        self._log(f"Error: {msg}")


class Engine:
    def __init__(self, emit):
        self._emit = emit
        self._cancel_event = threading.Event()
        self._active = threading.Event()        # a download thread is in flight
        self._stream_bytes: dict[str, tuple[int, int]] = {}
        self._last_progress = 0.0
        self._output: str | None = None
        self._partials: set[str] = set()     # .part files yt-dlp is writing
        self._frames_gen = 0                 # bumps per local preview; stale frame jobs stop

    # ------------------------------------------------------------ public --

    @property
    def running(self) -> bool:
        return self._active.is_set()

    def start(self, raw: dict) -> dict:
        """Validate a request from the UI and start it. Returns {"ok": bool, "error": str}."""
        if self._active.is_set():
            return {"ok": False, "error": "A download is already running."}

        url = (raw.get("url") or "").strip()
        if not url:
            return {"ok": False, "error": "Paste a link or open a video file first.", "field": "url"}
        local = localfile.as_path(url)
        outdir = (raw.get("outdir") or "").strip()
        if not outdir:
            return {"ok": False, "error": "Choose a folder to save into.", "field": "outdir"}

        start_time = end_time = None
        if raw.get("clip"):
            start_raw, end_raw = raw.get("start") or "", raw.get("end") or ""
            start_time, end_time = parse_time(start_raw), parse_time(end_raw)
            if start_raw.strip() and start_time is None:
                return {"ok": False, "error": "Start time should look like 1:30 or 1:02:03.", "field": "start"}
            if end_raw.strip() and end_time is None:
                return {"ok": False, "error": "End time should look like 1:30 or 1:02:03.", "field": "end"}
            if start_time is not None and end_time is not None and end_time <= start_time:
                return {"ok": False, "error": "The clip has to end after it starts.", "field": "end"}

        fmt_name = raw.get("format") if raw.get("format") in FORMATS else "Best Quality"
        cookie_source = raw.get("cookie_source") if raw.get("cookie_source") in ("None", "Browser", "File") else "None"
        if not local and cookie_source == "File" and not (raw.get("cookiefile") or "").strip():
            return {"ok": False, "error": "Choose a cookies.txt file, or set cookies to None.", "field": "cookiefile"}

        cfg = {
            "url": url,
            "local": local,
            "outdir": outdir,
            "start_time": start_time,
            "end_time": end_time,
            "fmt_name": fmt_name,
            "whole_playlist": bool(raw.get("whole_playlist")),
            "precise": bool(raw.get("precise")),
            "cookie_source": cookie_source,
            "browser": raw.get("browser") if raw.get("browser") in BROWSERS else "chrome",
            "cookiefile": (raw.get("cookiefile") or "").strip(),
        }

        self._cancel_event.clear()
        self._active.set()
        threading.Thread(target=self._do_download, args=(cfg,), daemon=True).start()
        return {"ok": True}

    def cancel(self):
        if not self._active.is_set():
            return
        self._cancel_event.set()
        self._log("Cancelling…")
        # The native downloader notices the flag on its next progress callback, but a
        # clip download is parked in a blocking ffmpeg subprocess that never calls
        # back — that one has to be killed outright.
        threading.Thread(target=self._kill_ffmpeg, daemon=True).start()

    def shutdown(self):
        """Window is closing: don't leave orphaned ffmpeg processes behind."""
        self._cancel_event.set()
        self._active.clear()
        self._sweep_ffmpeg()

    def preview(self, raw: dict) -> dict:
        """Title, thumbnail, duration and storyboards for the preview card. Blocking."""
        url = (raw.get("url") or "").strip()
        local = localfile.as_path(url)
        if local:
            return self._preview_local(local)
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": not raw.get("whole_playlist"),
            "extract_flat": "in_playlist",
            "ignore_no_formats_error": True,
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
            "logger": _Logger(lambda _m: None),
        }
        try:
            self._apply_cookies(opts, {
                "cookie_source": raw.get("cookie_source") or "None",
                "browser": raw.get("browser") or "chrome",
                "cookiefile": (raw.get("cookiefile") or "").strip(),
            })
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
        except Exception as exc:
            msg = str(exc).replace("ERROR: ", "").strip()
            return {"ok": False, "error": msg.splitlines()[0][:300] if msg else "Couldn't read that link."}

        if info.get("_type") in ("playlist", "multi_video"):
            entries = list(info.get("entries") or [])
            thumb = info.get("thumbnail")
            if not thumb:
                for e in entries:
                    thumbs = e.get("thumbnails") or []
                    if thumbs and thumbs[-1].get("url"):
                        thumb = thumbs[-1]["url"]
                        break
            return {
                "ok": True, "kind": "playlist",
                "title": info.get("title") or "Playlist",
                "channel": info.get("channel") or info.get("uploader") or "",
                "count": info.get("playlist_count") or len(entries),
                "thumbnail": thumb,
            }

        return {
            "ok": True, "kind": "video",
            "title": info.get("title") or "Untitled",
            "channel": info.get("channel") or info.get("uploader") or "",
            "duration": info.get("duration"),
            "views": info.get("view_count"),
            "upload_date": info.get("upload_date"),
            "thumbnail": info.get("thumbnail"),
            "is_live": bool(info.get("is_live")),
            "storyboards": self._storyboards(info),
        }

    # ------------------------------------------------------------ events --

    def _log(self, msg: str):
        self._emit("log", {"msg": msg})

    def _status(self, text: str):
        self._emit("status", {"text": text})

    def _progress(self, fraction, speed="", eta="", detail=""):
        now = time.monotonic()
        # Hooks fire many times a second; the UI only needs ~10 updates/s.
        if fraction not in (None, 1.0) and now - self._last_progress < 0.1:
            return
        self._last_progress = now
        self._emit("progress", {"fraction": fraction, "speed": speed, "eta": eta, "detail": detail})

    def _finish(self, state: str, message: str = "", path: str | None = None):
        self._emit("finished", {"state": state, "message": message, "path": path})

    # ---------------------------------------------------------- download --

    def _do_download(self, cfg: dict):
        self._output = None
        self._partials.clear()
        self._log(f"Starting: {cfg['url']}")
        if cfg.get("local"):
            return self._do_local(cfg)
        # On a 403, step down this ladder. YouTube often refuses stream requests from
        # logged-in (cookie) sessions, and lately refuses direct DASH URLs at random
        # while still serving HLS. Cookies still matter for age-gated videos, so
        # they're dropped only after a 403.
        ladder = [cfg]
        if cfg["cookie_source"] != "None":
            ladder.append({**cfg, "cookie_source": "None"})
        ladder.append({**ladder[-1], "prefer_hls": True})

        try:
            for i, rung in enumerate(ladder):
                try:
                    self._attempt(rung)
                    break
                except Exception as exc:
                    last = i == len(ladder) - 1
                    if last or not _is_forbidden(exc) or self._cancel_event.is_set():
                        raise
                    if ladder[i + 1]["cookie_source"] != rung["cookie_source"]:
                        self._log("Got 403 Forbidden with cookies — YouTube often rejects "
                                  "logged-in stream requests. Retrying without cookies…")
                        self._status("Retrying without cookies…")
                    else:
                        self._log("YouTube refused the direct stream (403) — retrying via HLS…")
                        self._status("Retrying via HLS…")
            self._progress(1.0)
            self._finish("done", "Saved", self._output)
        except (yt_dlp.utils.DownloadCancelled, clipper.ClipCancelled):
            self._remove_partials()
            self._finish("cancelled", "Cancelled")
        except Exception as exc:
            # Killing ffmpeg surfaces as a generic non-zero-exit DownloadError, so
            # check our own flag before calling this an error.
            if self._cancel_event.is_set():
                self._remove_partials()
                self._finish("cancelled", "Cancelled")
            else:
                msg = str(exc).replace("ERROR: ", "").strip()
                self._log(f"Error: {msg}")
                if "Could not copy" in msg and "cookie" in msg.lower():
                    self._log(
                        "Hint: Chrome locks its cookie DB while running.\n"
                        "Fix: add  --disable-features=LockProfileCookieDatabase  to your Chrome shortcut,\n"
                        "     restart Chrome, then try again. Or close Chrome before downloading.")
                    msg = "Chrome has its cookie database locked — close Chrome or set cookies to None."
                self._finish("error", msg.splitlines()[0][:300] if msg else "Download failed")
        finally:
            self._active.clear()          # lets any sweeping cancel thread stop

    def _attempt(self, cfg: dict):
        """One full try at the download, via the fast clip path where it applies."""
        clipping = cfg["start_time"] is not None or cfg["end_time"] is not None
        self._stream_bytes.clear()

        if clipping and not cfg.get("prefer_hls") and self._clip_via_index(cfg):
            return
        if clipping:
            # Fallback path: yt-dlp hands the clip to FFmpegFD, which runs ffmpeg
            # as one blocking subprocess with no progress callbacks — and which
            # stalls outright on some CDNs. Show indeterminate progress, watch for a hang.
            self._status("Clipping with ffmpeg…")
            self._progress(None, detail="ffmpeg doesn't report progress for clips")
            threading.Thread(target=self._stall_watchdog, daemon=True).start()
        self._download_via_ytdlp(cfg, clipping)

    def _apply_cookies(self, opts: dict, cfg: dict):
        source = cfg["cookie_source"]
        if source == "Browser":
            opts["cookiesfrombrowser"] = (cfg["browser"], None, None, None)
        elif source == "File":
            if not cfg["cookiefile"]:
                raise RuntimeError("no cookies file path set — choose one, or switch to None")
            opts["cookiefile"] = cfg["cookiefile"]

    def _base_opts(self, cfg: dict) -> dict:
        """yt-dlp options common to both download paths."""
        opts = {
            "logger": _Logger(self._log),
            "quiet": True,
            "no_warnings": False,
            # A bare watch?v=…&list=… URL pulls the entire playlist unless we say otherwise.
            "noplaylist": not cfg["whole_playlist"],
            # Node.js is installed — use it for the n-challenge, fetching the EJS
            # solver from GitHub on first run (cached after that).
            "js_runtimes": {"node": {}},
            "remote_components": {"ejs:github"},
        }
        self._apply_cookies(opts, cfg)
        return opts

    def _clip_via_index(self, cfg: dict) -> bool:
        """Fetch only the segments covering the clip. False means fall back to yt-dlp.

        ffmpeg cannot pull YouTube's DASH streams — googlevideo stalls its long
        sequential GETs forever — so we read the sidx index and fetch the byte range
        ourselves through yt-dlp's HTTP stack, which the CDN serves at full speed.
        """
        opts = self._base_opts(cfg)
        opts["format"] = CLIP_FORMATS[cfg["fmt_name"]]

        start = cfg["start_time"] or 0.0
        end = cfg["end_time"]

        self._status("Looking up the video…")
        self._progress(None)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(cfg["url"], download=False)
                if end is None:                     # "from here to the end"
                    end = float(info.get("duration") or 0)
                if not end or end <= start:
                    self._log("Clip needs a known end time — falling back to ffmpeg.")
                    return False

                self._status(f"Fetching the {_fmt_eta(end - start)} clip…")
                self._progress(None, detail="Only the segments the clip needs")
                out = clipper.clip(
                    ydl, info, start, end, cfg["outdir"],
                    audio_only=cfg["fmt_name"] in AUDIO_ONLY_FORMATS,
                    precise=cfg["precise"],
                    log=self._log,
                    cancelled=self._cancel_event.is_set,
                    name_suffix=clip_suffix(start, cfg["end_time"]),
                )
        except clipper.ClipUnsupported as exc:
            self._log(f"No usable stream index ({exc}) — falling back to ffmpeg.")
            return False
        except Exception as exc:
            if not _is_forbidden(exc):
                raise
            # YouTube refuses direct DASH downloads for some videos and clients
            # while still serving HLS, which the fallback path prefers.
            self._log("Direct stream refused (403) — falling back to HLS via ffmpeg.")
            return False

        self._output = str(out)
        self._log(f"Done! {out.name}")
        return True

    def _download_via_ytdlp(self, cfg: dict, clipping: bool):
        fmt_name = cfg["fmt_name"]
        opts = self._base_opts(cfg)
        opts.update({
            **FORMATS[fmt_name],
            "outtmpl": str(Path(cfg["outdir"]) / (
                "%(title)s" + (clip_suffix(cfg["start_time"], cfg["end_time"]) if clipping else "") + ".%(ext)s")),
            "progress_hooks": [self._progress_hook],
            "postprocessor_hooks": [self._postprocessor_hook],
        })
        if fmt_name not in AUDIO_ONLY_FORMATS:
            opts["merge_output_format"] = "mp4"
        if clipping or cfg.get("prefer_hls"):
            opts["format"] = HLS_FORMATS[fmt_name]

        if clipping:
            s = cfg["start_time"] if cfg["start_time"] is not None else 0.0
            # 0, not float('inf'), for "run to the end": yt-dlp treats a falsy
            # section_end as open-ended and omits ffmpeg's -t. Passing inf emits a
            # literal `-t inf`, which ffmpeg rejects ("Invalid duration for option t").
            e = cfg["end_time"] if cfg["end_time"] is not None else 0
            opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(s, e)])
            # Off by default: with this on, yt-dlp drops ffmpeg's `-c copy` and
            # re-encodes the clip. Accurate to the frame, but far slower.
            opts["force_keyframes_at_cuts"] = cfg["precise"]
        else:
            self._status("Looking up the video…")
            self._progress(None)

        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.add_post_hook(self._on_file_done)      # final path, after merging/converting
            ret = ydl.download([cfg["url"]])
        if ret != 0:
            raise RuntimeError(f"yt-dlp finished with return code {ret}")
        self._log("Done!")

    def _remove_partials(self):
        """yt-dlp leaves its half-written .part files behind on cancel; tidy them."""
        for path in self._partials:
            if path.endswith(".part"):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._partials.clear()

    def _on_file_done(self, filepath: str):
        self._output = filepath

    # ------------------------------------------------------------- hooks --

    def _progress_hook(self, d: dict):
        # Record the temp file before honouring a cancel, or a cancel that lands on
        # the very first callback leaves an orphaned .part behind.
        if d.get("tmpfilename"):
            self._partials.add(d["tmpfilename"])
        if self._cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled()

        status = d.get("status")
        fname  = Path(d.get("filename", "")).name

        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            # bestvideo+bestaudio arrives as two separate downloads. Track each stream
            # and show the combined total, so the bar climbs once instead of twice.
            self._stream_bytes[fname] = (d.get("downloaded_bytes", 0), total)
            done   = sum(v[0] for v in self._stream_bytes.values())
            wanted = sum(v[1] for v in self._stream_bytes.values())
            speed = d.get("speed")
            self._status("Downloading…")
            self._progress(
                min(done / wanted, 1.0) if wanted else None,
                speed=f"{_fmt_bytes(speed)}/s" if speed else "",
                eta=_fmt_eta(d.get("eta")),
                detail=f"{_fmt_bytes(done)} of {_fmt_bytes(wanted)}" if wanted else _fmt_bytes(done),
            )

        elif status == "finished":
            self._status("Processing…")

    def _postprocessor_hook(self, d: dict):
        # Gives Cancel something to bite on during long merges and MP3 extraction,
        # which happen after the last progress_hook fires.
        if self._cancel_event.is_set():
            raise yt_dlp.utils.DownloadCancelled()
        if d.get("status") == "started":
            name = {"Merger": "Merging audio and video", "FFmpegExtractAudio": "Converting to MP3",
                    "MoveFiles": "Finishing up"}.get(d.get("postprocessor", ""), "Processing")
            self._status(f"{name}…")

    # -------------------------------------------------------- local files --

    def _preview_local(self, path: str) -> dict:
        try:
            info = localfile.probe(path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)[:300]}
        duration = info["duration"]
        self._frames_gen += 1
        gen = self._frames_gen
        thumb = None
        if info["has_video"]:
            thumb = localfile.frame(path, min(duration * 0.1, 60) if duration else 0, 320, 180)
            if duration:
                threading.Thread(target=self._local_frames, args=(path, duration, gen), daemon=True).start()
        try:
            size = _fmt_bytes(os.path.getsize(path))
        except OSError:
            size = ""
        stats = [f"{info['width']}×{info['height']}" if info["has_video"] else "Audio only", size]
        return {
            "ok": True, "kind": "file", "path": path,
            "title": Path(path).name,
            "channel": str(Path(path).parent),
            "duration": duration,
            "stats": " · ".join(s for s in stats if s),
            "thumbnail": thumb,
            "is_live": False,
            "storyboards": [],
        }

    def _local_frames(self, path: str, duration: float, gen: int):
        board = localfile.storyboard(path, duration, stopped=lambda: gen != self._frames_gen)
        if board and gen == self._frames_gen:
            self._emit("frames", {"path": path, "board": board})

    def _do_local(self, cfg: dict):
        try:
            out = self._trim_local(cfg)
            self._output = str(out)
            self._progress(1.0)
            self._finish("done", "Saved", self._output)
        except clipper.ClipCancelled:
            self._finish("cancelled", "Cancelled")
        except Exception as exc:
            msg = str(exc).strip()
            self._log(f"Error: {msg}")
            self._finish("error", msg.splitlines()[0][:300] if msg else "Couldn't save the clip")
        finally:
            self._active.clear()

    def _trim_local(self, cfg: dict) -> Path:
        src = cfg["local"]
        info = localfile.probe(src)
        total = info["duration"]
        start = cfg["start_time"] or 0.0
        end = cfg["end_time"]
        if total:
            if start >= total:
                raise ValueError(f"The video is only {_fmt_eta(total)} long.")
            if end is not None:
                end = min(end, total)
        span = (end if end is not None else total or 0) - start
        clipping = cfg["start_time"] is not None or cfg["end_time"] is not None

        audio_only = cfg["fmt_name"] in AUDIO_ONLY_FORMATS
        if audio_only and not info["has_audio"]:
            raise ValueError("This file has no audio to save as MP3.")
        cap = {"1080p": 1080, "720p": 720, "480p": 480}.get(cfg["fmt_name"])
        short_side = min(info["width"] or 0, info["height"] or 0)
        scale = bool(cap and info["has_video"] and short_side > cap)
        reencode = not audio_only and info["has_video"] and (cfg["precise"] or scale)

        src_path = Path(src)
        if audio_only:
            ext = ".mp3"
        elif reencode:
            ext = ".mp4"
        else:
            ext = src_path.suffix.lower() if src_path.suffix.lower() in localfile.COPY_EXTS else ".mkv"
        name = src_path.stem + (clip_suffix(start, cfg["end_time"]) if clipping else "") + \
            (f" ({cap}p)" if scale else "")
        outdir = Path(cfg["outdir"])
        outdir.mkdir(parents=True, exist_ok=True)
        out = clipper._unique(outdir / f"{name}{ext}")
        tmp = out.with_name(f"{out.stem}.part{out.suffix}")

        what = ("Converting to MP3" if audio_only else
                f"Re-encoding to {cap}p" if scale else
                "Re-encoding for an exact cut" if reencode else
                "Copying without re-encoding")
        span_txt = f"{_fmt_eta(start)}–{_fmt_eta(start + span)}" if clipping else "whole file"
        self._log(f"  {what} ({span_txt})…")
        self._status(f"{what}…")

        args = localfile.trim_command(src, str(tmp), start, span if clipping else None,
                                      audio_only=audio_only, reencode=reencode,
                                      cap=cap if scale else None, info=info)
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace",
                                    creationflags=localfile.NO_WINDOW)
        except FileNotFoundError:
            raise RuntimeError("ffmpeg wasn't found. Install it and put it on your PATH.")
        errors: list[str] = []
        threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()

        speed = ""
        try:
            for line in proc.stdout:
                if self._cancel_event.is_set():
                    proc.kill()
                    break
                key, _, value = line.strip().partition("=")
                if key == "speed":
                    speed = value if value not in ("N/A", "") else ""
                elif key == "out_time_us" and span > 0 and value.lstrip("-").isdigit():
                    done = max(0, int(value)) / 1e6
                    self._progress(min(done / span, 0.999), speed=speed,
                                   detail=f"{_fmt_eta(done)} of {_fmt_eta(span)}")
            proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        if self._cancel_event.is_set():
            tmp.unlink(missing_ok=True)
            raise clipper.ClipCancelled()
        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            detail = "".join(errors).strip().splitlines()
            raise RuntimeError(f"ffmpeg failed: {detail[-1] if detail else f'exit code {proc.returncode}'}")
        os.replace(tmp, out)
        self._log(f"Done! {out.name}")
        return out

    # ------------------------------------------------------------ ffmpeg --

    def _kill_ffmpeg(self):
        """Kill the ffmpeg child behind a clip download and bin its .part file.

        Cancel can land before ffmpeg has even been spawned — during extraction, say —
        so keep sweeping until something dies or the download thread gives up.
        """
        while self._active.is_set():
            if self._sweep_ffmpeg():
                return
            time.sleep(0.25)

    def _stall_watchdog(self):
        """Give up on an ffmpeg that has stopped producing output.

        FFmpegFD hangs indefinitely on CDNs that stall its sequential GETs, which
        used to leave processes running for hours after a failed clip.
        """
        stalled, previous = 0, {}
        while self._active.is_set():
            time.sleep(10)
            if not self._active.is_set():
                return
            sizes = self._ffmpeg_output_sizes()
            stalled = stalled + 10 if sizes and sizes == previous else 0
            previous = sizes
            if stalled >= STALL_TIMEOUT:
                self._log(f"ffmpeg produced no data for {STALL_TIMEOUT}s — giving up.")
                self._cancel_event.set()
                self._sweep_ffmpeg()
                return

    def _ffmpeg_output_sizes(self) -> dict:
        """Current size of every .part file our ffmpeg children are writing."""
        try:
            import psutil
        except ImportError:
            return {}
        sizes = {}
        try:
            for child in psutil.Process().children(recursive=True):
                try:
                    if "ffmpeg" not in (child.name() or "").lower():
                        continue
                    cmd = child.cmdline()
                    if cmd and cmd[-1].endswith(".part"):
                        sizes[cmd[-1]] = os.path.getsize(cmd[-1])
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
        except psutil.Error:
            pass
        return sizes

    def _sweep_ffmpeg(self) -> bool:
        try:
            import psutil
        except ImportError:
            return True   # nothing we can do; stop sweeping

        victims, leftovers = [], []
        try:
            for child in psutil.Process().children(recursive=True):
                try:
                    if "ffmpeg" not in (child.name() or "").lower():
                        continue
                    cmd = child.cmdline()
                    if cmd and cmd[-1].endswith(".part"):
                        leftovers.append(cmd[-1])
                    child.terminate()
                    victims.append(child)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except psutil.Error:
            return False

        if not victims:
            return False

        psutil.wait_procs(victims, timeout=5)   # Windows keeps the file locked until it exits
        for path in leftovers:
            try:
                os.remove(path)
            except OSError:
                pass
        return True

    # -------------------------------------------------------- storyboards --

    @staticmethod
    def _storyboards(info: dict) -> list:
        """YouTube's frame-grid images, for the clip timeline's frame previews.

        Each fragment is a sprite sheet of cols x rows frames, one frame every
        1/fps seconds. Returned largest first.
        """
        boards = []
        for f in info.get("formats") or []:
            if not str(f.get("format_id", "")).startswith("sb"):
                continue
            frags = [fr.get("url") for fr in (f.get("fragments") or []) if fr.get("url")]
            if not frags or not f.get("fps") or not f.get("columns") or not f.get("rows"):
                continue
            boards.append({
                "width": f.get("width"), "height": f.get("height"),
                "cols": f["columns"], "rows": f["rows"],
                "interval": 1 / f["fps"],
                "urls": frags,
            })
        boards.sort(key=lambda b: -(b["width"] or 0))
        return boards
