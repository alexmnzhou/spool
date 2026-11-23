"""Clip a time range out of a DASH stream without letting ffmpeg touch the network.

yt-dlp hands --download-sections to ffmpeg, but googlevideo stalls ffmpeg's long
sequential GETs indefinitely — a clip request simply hangs forever. yt-dlp's own
chunked ranged reads against the identical URL run at full speed, so we do the
fetching ourselves: read the `sidx` index, work out which byte range covers the
requested window, pull just those segments, and let ffmpeg trim the local file.

Falls back by raising ClipUnsupported when a stream carries no usable index.
"""
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path

from yt_dlp.networking import Request
from yt_dlp.utils import sanitize_filename

HEAD_BYTES = 400_000        # enough for ftyp + moov + a large sidx
MAX_INDEX_SEGMENTS = 100_000


class ClipUnsupported(Exception):
    """This stream has no usable sidx index; the caller should fall back."""


class ClipCancelled(Exception):
    """The user cancelled partway through."""


def _fetch(ydl, fmt, first, last):
    headers = {**(fmt.get("http_headers") or {}), "Range": f"bytes={first}-{last}"}
    resp = ydl.urlopen(Request(fmt["url"], headers=headers))
    try:
        return resp.read()
    finally:
        resp.close()


def _parse_head(head):
    """Return (init_bytes, [(size, duration_ticks)], timescale, first_segment_offset).

    init_bytes is ftyp+moov only — the sidx is deliberately excluded, since an
    index describing segments we don't ship would be inconsistent.
    """
    init, segments, timescale, seg_off, off = b"", [], None, None, 0

    while off + 8 <= len(head):
        size = struct.unpack(">I", head[off:off + 4])[0]
        btype = head[off + 4:off + 8].decode("latin-1", "replace")
        if size == 1:
            if off + 16 > len(head):
                break
            size = struct.unpack(">Q", head[off + 8:off + 16])[0]
        if size < 8:
            break

        if btype in ("ftyp", "moov"):
            if off + size > len(head):
                raise ClipUnsupported("init segment truncated")
            init += head[off:off + size]
        elif btype == "sidx":
            if off + size > len(head):
                raise ClipUnsupported("sidx larger than probe window")
            segments, timescale = _parse_sidx(head, off)
        elif btype in ("moof", "mdat"):
            seg_off = off
            break
        off += size

    return init, segments, timescale, seg_off


def _parse_sidx(head, off):
    version = head[off + 8]
    p = off + 12 + 4                                    # version/flags, reference_ID
    timescale = struct.unpack(">I", head[p:p + 4])[0]
    p += 4
    p += 8 if version == 0 else 16                      # earliest_pts + first_offset
    p += 2                                              # reserved
    count = struct.unpack(">H", head[p:p + 2])[0]
    p += 2
    if not count or count > MAX_INDEX_SEGMENTS:
        raise ClipUnsupported(f"implausible sidx entry count {count}")
    if not timescale:
        raise ClipUnsupported("sidx timescale is zero")

    segments = []
    for _ in range(count):
        ref, dur, _sap = struct.unpack(">III", head[p:p + 12])
        p += 12
        segments.append((ref & 0x7FFF_FFFF, dur))
    return segments, timescale


def _byte_range(segments, timescale, base_off, start, end):
    """Byte range of the segments covering [start, end], plus that range's real start time."""
    t, pos = 0.0, base_off
    first_off = first_time = None

    for size, dur in segments:
        seg_start = t
        seg_end = t + dur / timescale
        if first_off is None and seg_end > start:
            first_off, first_time = pos, seg_start
        if first_off is not None and seg_start >= end:
            return first_off, pos - 1, first_time
        t, pos = seg_end, pos + size

    if first_off is None:
        raise ClipUnsupported(f"start {start:.0f}s is past the end of the index")
    return first_off, pos - 1, first_time          # window runs to end of stream


def _grab(ydl, fmt, start, end, dest, on_progress):
    head = _fetch(ydl, fmt, 0, HEAD_BYTES)
    init, segments, timescale, seg_off = _parse_head(head)
    if not segments or seg_off is None:
        raise ClipUnsupported("no sidx index in this stream")
    if not init:
        raise ClipUnsupported("no ftyp/moov init segment")

    lo, hi, first_time = _byte_range(segments, timescale, seg_off, start, end)
    body = _fetch(ydl, fmt, lo, hi)
    dest.write_bytes(init + body)
    on_progress((len(init) + len(body)) / 1_048_576)
    return first_time


def clip(ydl, info, start, end, outdir, audio_only=False, precise=False,
         log=lambda _m: None, cancelled=lambda: False, name_suffix=""):
    """Fetch and trim [start, end]. Returns the finished file's Path.

    With precise=False the trim is a stream copy, so it starts at the nearest
    keyframe at or before `start` — segments run ~5s, so the clip can begin a
    little early. precise=True re-encodes for a frame-exact cut.
    """
    streams = info.get("requested_formats") or [info]
    duration = end - start
    tmpdir = Path(tempfile.gettempdir())
    token = uuid.uuid4().hex[:8]
    temps = []

    try:
        parts = []
        for fmt in streams:
            if cancelled():
                raise ClipCancelled()
            kind = "audio" if (fmt.get("vcodec") or "none") == "none" else "video"
            dest = tmpdir / f"spool_{token}_{kind}.mp4"
            temps.append(dest)
            first_time = _grab(
                ydl, fmt, start, end, dest,
                lambda mb, k=kind, f=fmt: log(f"  {k}: fetched {mb:.2f} MB "
                                              f"(format {f.get('format_id')})"))
            parts.append((dest, first_time))

        if cancelled():
            raise ClipCancelled()

        title = sanitize_filename(info.get("title") or "clip", restricted=False)
        out = Path(outdir) / f"{title}{name_suffix}.{'mp3' if audio_only else 'mp4'}"
        out = _unique(out)

        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for path, first_time in parts:
            # Seek relative to where the fetched segments actually begin.
            args += ["-ss", f"{max(0.0, start - first_time):.3f}",
                     "-t", f"{duration:.3f}", "-i", str(path)]
        for i in range(len(parts)):
            args += ["-map", f"{i}:0"]
        if audio_only:
            args += ["-c:a", "libmp3lame", "-b:a", "192k"]
        elif precise:
            args += ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
                     "-c:a", "aac", "-b:a", "192k"]
        else:
            args += ["-c", "copy"]
        args += [str(out)]

        log("  re-encoding for an exact cut…" if precise and not audio_only else "  trimming…")
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg trim failed: {proc.stderr.strip()[:400]}")
        return out
    finally:
        for path in temps:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _unique(path: Path) -> Path:
    """Don't silently clobber an existing clip of the same video."""
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        if not candidate.exists():
            return candidate
    return path
