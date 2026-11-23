"""Spool — a friendlier face for yt-dlp.

The window is a pywebview page (ui/), and the downloading lives in engine.py.
This file is the bridge: Api's public methods are callable from the page's
JavaScript as window.pywebview.api.<name>(), and engine events are pushed back
into the page through window.spool.onEvent().
"""
import ctypes
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import webview
import yt_dlp.version

from engine import BROWSERS, DEFAULT_COOKIES_FILE, FORMATS, Engine

HERE = Path(__file__).resolve().parent
UI_PAGE = HERE / "ui" / "index.html"
ICON = HERE / "icon.ico"
SETTINGS_FILE = HERE / "settings.json"
BACKGROUND = "#0d0d10"          # matches --bg in ui/style.css, avoids a white flash

DEFAULT_SETTINGS = {
    "outdir": str(Path.home() / "Downloads"),
    "format": "Best Quality",
    # YouTube rejects most stream requests from logged-in sessions these days, so
    # cookies are opt-in for the odd age-restricted video.
    "cookie_source": "None",
    "browser": "chrome",
    "cookiefile": str(DEFAULT_COOKIES_FILE) if DEFAULT_COOKIES_FILE.exists() else "",
    "precise": False,
    "whole_playlist": False,
}


def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    if settings["format"] not in FORMATS:
        settings["format"] = DEFAULT_SETTINGS["format"]
    return settings


def clipboard_text() -> str:
    """Read text from the Windows clipboard without pulling in another dependency."""
    if sys.platform != "win32":
        return ""
    CF_UNICODETEXT = 13
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def style_title_bar(window):
    """Dark title bar in our background colour, so the window reads as one piece (Win 11)."""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.c_void_p(window.native.Handle.ToInt64())
        dwm = ctypes.windll.dwmapi
        dark = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(dark), ctypes.sizeof(dark))      # immersive dark mode
        r, g, b = (int(BACKGROUND[i:i + 2], 16) for i in (1, 3, 5))
        caption = ctypes.c_int(r | (g << 8) | (b << 16))                                     # COLORREF is 0x00BBGGRR
        dwm.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption))  # caption colour
    except Exception:
        pass        # cosmetic only; older Windows just keeps the default bar


class Api:
    """Everything the page can call. Underscore names are hidden from JavaScript."""

    def __init__(self):
        self._window = None
        self._settings = load_settings()
        self._engine = Engine(self._emit)

    def _emit(self, name: str, payload: dict):
        window = self._window
        if window is None:
            return
        try:
            window.run_js(f"window.spool && window.spool.onEvent({json.dumps(name)}, {json.dumps(payload)})")
        except Exception:
            pass        # window is closing

    # -- called from the page ------------------------------------------------

    def init(self):
        return {
            "settings": self._settings,
            "browsers": BROWSERS,
            "versions": {"yt_dlp": yt_dlp.version.__version__, "python": platform.python_version()},
        }

    def save_settings(self, settings):
        self._settings.update({k: v for k, v in (settings or {}).items() if k in DEFAULT_SETTINGS})
        try:
            SETTINGS_FILE.write_text(json.dumps(self._settings, indent=2), encoding="utf-8")
        except OSError:
            pass

    def preview(self, request):
        return self._engine.preview(request or {})

    def start(self, request):
        return self._engine.start(request or {})

    def cancel(self):
        self._engine.cancel()

    def pick_folder(self, current=""):
        start = current if current and os.path.isdir(current) else str(Path.home())
        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
        return result[0] if result else None

    def pick_cookie_file(self):
        result = self._window.create_file_dialog(
            webview.FileDialog.OPEN, directory=str(HERE),
            file_types=("Cookie files (*.txt)", "All files (*.*)"))
        return result[0] if result else None

    def paste(self):
        try:
            return clipboard_text()
        except Exception:
            return ""

    def open_path(self, path):
        if path and os.path.exists(path):
            os.startfile(path)

    def reveal(self, path):
        if path and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif path and os.path.isdir(os.path.dirname(path)):
            os.startfile(os.path.dirname(path))


def main():
    api = Api()
    window = webview.create_window(
        "Spool",
        url=str(UI_PAGE),
        js_api=api,
        width=820, height=720, min_size=(640, 520),
        background_color=BACKGROUND,
        text_select=True,       # the page opts specific elements in and out itself
    )
    api._window = window
    window.events.shown += lambda: style_title_bar(window)
    window.events.closing += api._engine.shutdown      # never orphan ffmpeg
    webview.start(icon=str(ICON), debug=bool(os.environ.get("SPOOL_DEBUG")))


if __name__ == "__main__":
    main()
