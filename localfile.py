"""Clipping videos that are already on disk: probe with ffprobe, grab preview
frames for the timeline, and build the ffmpeg trim command.

Local files need none of the streaming tricks in clipper.py: ffmpeg seeks a
local file instantly, so `-ss` before `-i` plus `-c copy` cuts a few minutes
out of a multi-hour recording in about a second.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# pythonw has no console, so every console child would flash a window without this.
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

FRAMES = 60                 # preview frames across the timeline
FRAME_W, FRAME_H = 160, 90  # matches the hover preview in the UI

# Containers we can stream-copy into as-is; anything else is copied into .mkv.
COPY_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".ts", ".mts", ".m2ts", ".avi",
             ".flv", ".m4a", ".mp3", ".wav", ".flac", ".ogg", ".opus", ".aac"}

VIDEO_PATTERNS = "*.mp4;*.mkv;*.mov;*.webm;*.m4v;*.avi;*.ts;*.mts;*.m2ts;*.flv;*.wmv"
AUDIO_PATTERNS = "*.mp3;*.m4a;*.wav;*.flac;*.ogg;*.opus;*.aac"


def as_path(text: str) -> str | None:
    """The existing file a typed or pasted string names, else None.

    Accepts Explorer's "Copy as path" (which adds quotes) and file:/// URLs.
    """
    s = (text or "").strip().strip('"').strip()
    if s.lower().startswith("file:///"):
        s = urllib.parse.unquote(s[8:])
    if not s or "://" in s:
        return None
    try:
        p = Path(os.path.expandvars(os.path.expanduser(s)))
        return str(p.resolve()) if p.is_file() else None
    except (OSError, ValueError):
        return None


def probe(path: str) -> dict:
    """Duration and picture size via ffprobe. Raises ValueError if it isn't media."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=NO_WINDOW, timeout=30)
    except FileNotFoundError:
        raise ValueError("ffprobe wasn't found. Install ffmpeg and put it on your PATH.")
    if r.returncode != 0:
        lines = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
        raise ValueError(lines[-1] if lines else "This doesn't look like a video or audio file.")
    data = json.loads(r.stdout or "{}")
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if not video and not has_audio:
        raise ValueError("This file has no video or audio in it.")
    try:
        duration = float((data.get("format") or {}).get("duration") or 0) or None
    except ValueError:
        duration = None
    return {
        "duration": duration,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "has_video": bool(video),
        "has_audio": has_audio,
    }


def frame(path: str, t: float, w: int = FRAME_W, h: int = FRAME_H) -> str | None:
    """One frame at time t, letterboxed to w x h, as a JPEG data URI."""
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", path,
             "-frames:v", "1", "-vf", vf, "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"],
            capture_output=True, creationflags=NO_WINDOW, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(r.stdout).decode()


def storyboard(path: str, duration: float, stopped=lambda: False) -> dict | None:
    """Evenly spaced frames in the same shape as YouTube's storyboards (see the UI's
    frameAt): one frame per "sheet", so cols = rows = 1."""
    interval = duration / FRAMES
    times = [(i + 0.5) * interval for i in range(FRAMES)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        urls = list(pool.map(lambda t: None if stopped() else frame(path, t), times))
    if stopped() or not any(urls):
        return None
    # A frame can fail near the very end of a file; reuse its neighbour.
    last = next(u for u in urls if u)
    for i, u in enumerate(urls):
        urls[i] = last = u or last
    return {"width": FRAME_W, "height": FRAME_H, "cols": 1, "rows": 1,
            "interval": interval, "urls": urls}


def trim_command(src: str, out: str, start: float, duration: float | None, *,
                 audio_only: bool, reencode: bool, cap: int | None, info: dict) -> list:
    """ffmpeg arguments for one trim. Progress goes to stdout (-progress pipe:1)."""
    args = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error"]
    if start:
        args += ["-ss", f"{start:.3f}"]          # before -i: jump straight there
    args += ["-i", src]
    if duration:
        args += ["-t", f"{duration:.3f}"]
    if audio_only:
        args += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", "192k"]
    elif reencode:
        args += ["-map", "0:v:0", "-map", "0:a?"]
        if cap:
            # Cap the short side, so vertical videos come out 1080x1920, not 608x1080.
            portrait = (info.get("height") or 0) > (info.get("width") or 0)
            args += ["-vf", f"scale={cap}:-2" if portrait else f"scale=-2:{cap}"]
        args += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    else:
        # Every audio track is kept (screen recordings often have several).
        args += ["-map", "0:v:0?", "-map", "0:a?", "-c", "copy", "-avoid_negative_ts", "make_zero"]
    args += ["-progress", "pipe:1", "-nostats", out]
    return args
