# /opt/ai/projects/course_web/system_tools.py
# -*- coding: utf-8 -*-
"""
system_tools.py - کارهایی که با سیستم‌عامل کاربر سروکار دارند

  * انتخاب پوشه: پنجره‌ی بومی سیستم (zenity / kdialog / tkinter / osascript)
    و در صورت نبودن آن‌ها، فهرست‌کردن پوشه‌ها برای «مرورگر پوشه‌ی» داخل برنامه
  * پیدا کردن پلیرهای نصب‌شده و باز کردن ویدیو با آن‌ها
  * ثبت زمان توقف (تا دقیقه و ثانیه) فقط برای پلیرهایی که اجازه‌ی پرسیدن موقعیت را می‌دهند:
        mpv  -> IPC (سوکت یونیکس / named pipe در ویندوز)
        VLC  -> رابط HTTP (فقط روی 127.0.0.1 با رمز تصادفی)
    پلیرهای دیگر فقط باز می‌شوند.

برنامه فقط روی همین کامپیوتر (127.0.0.1) اجرا می‌شود، پس سرور و مرورگر روی یک سیستم‌اند.
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

import course_timer as ct


# ======================================================================
# انتخاب پوشه
# ======================================================================
_TK_SNIPPET = (
    "import sys, tkinter as tk\n"
    "from tkinter import filedialog\n"
    "r = tk.Tk(); r.withdraw()\n"
    "try:\n    r.attributes('-topmost', True)\n"
    "except Exception:\n    pass\n"
    "print(filedialog.askdirectory(initialdir=sys.argv[1], title=sys.argv[2], mustexist=True) or '')\n"
)


def _run(cmd: list, timeout: int = 900):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def pick_folder_native(initial: str = "", title: str = "انتخاب پوشه‌ی دوره"):
    """
    پنجره‌ی انتخاب پوشه‌ی خود سیستم را باز می‌کند (تا انتخاب یا انصراف منتظر می‌ماند).
    خروجی: ("ok", مسیر) | ("cancel", None) | ("unavailable", None)
    """
    start = initial if initial and os.path.isdir(initial) else str(Path.home())
    on_linux = sys.platform.startswith("linux")
    if on_linux and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return "unavailable", None

    tools: List[list] = []
    if sys.platform == "darwin":
        tools.append(["osascript", "-e", f'POSIX path of (choose folder with prompt "{title}")'])
    elif on_linux:
        tools.append(["zenity", "--file-selection", "--directory",
                      f"--title={title}", f"--filename={start.rstrip('/')}/"])
        tools.append(["kdialog", "--getexistingdirectory", start, "--title", title])
    tools.append([sys.executable, "-c", _TK_SNIPPET, start, title])

    for cmd in tools:
        if cmd[0] != sys.executable and shutil.which(cmd[0]) is None:
            continue
        proc = _run(cmd)
        if proc is None:
            continue
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0:
            if not out:
                return "cancel", None
            return "ok", os.path.normpath(out)
        if proc.returncode == 1 and (not err or "cancel" in err.lower()):
            return "cancel", None
        # ابزار خراب بود (مثلاً tkinter نصب نیست)؛ سراغ ابزار بعدی
    return "unavailable", None


def fs_roots() -> List[str]:
    if os.name == "nt":
        try:
            import ctypes
            mask = ctypes.windll.kernel32.GetLogicalDrives()  # type: ignore[attr-defined]
            return [f"{chr(65 + i)}:\\" for i in range(26) if mask & (1 << i)]
        except Exception:
            return ["C:\\"]
    return ["/"]


def list_dir(path: str = "") -> dict:
    """زیرپوشه‌های یک مسیر (برای مرورگر پوشه‌ی داخل برنامه). اگر مسیر وجود نداشت، نزدیک‌ترین پوشه‌ی بالاتر."""
    p = Path(os.path.abspath(os.path.expanduser(path))) if path else Path.home()
    while not p.is_dir() and p.parent != p:
        p = p.parent
    dirs, n_videos = [], 0
    with os.scandir(p) as it:                       # PermissionError را صدازننده مدیریت می‌کند
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "path": os.path.join(str(p), e.name)})
                elif e.is_file() and Path(e.name).suffix.lower() in ct.VIDEO_EXTENSIONS:
                    n_videos += 1
            except OSError:
                continue
    dirs.sort(key=lambda d: ct.natural_key(d["name"]))
    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else None,
        "dirs": dirs,
        "videos": n_videos,
        "home": str(Path.home()),
        "roots": fs_roots(),
    }


# ======================================================================
# پلیرها
# ======================================================================
def _expand(p: str) -> str:
    return (p.replace("{PF}", os.environ.get("ProgramFiles", r"C:\Program Files"))
             .replace("{PF86}", os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")))


# (id, نام، دستورها در PATH، مسیرهای معمول، ثبت زمان توقف دارد؟)
_CATALOG = [
    ("mpv", "mpv", ["mpv"],
     ["{PF}\\mpv\\mpv.exe", "/opt/homebrew/bin/mpv", "/usr/local/bin/mpv",
      "/Applications/mpv.app/Contents/MacOS/mpv"], True),
    ("vlc", "VLC", ["vlc"],
     ["{PF}\\VideoLAN\\VLC\\vlc.exe", "{PF86}\\VideoLAN\\VLC\\vlc.exe",
      "/Applications/VLC.app/Contents/MacOS/VLC", "/snap/bin/vlc"], True),
    ("smplayer", "SMPlayer", ["smplayer"], ["{PF}\\SMPlayer\\smplayer.exe", "{PF86}\\SMPlayer\\smplayer.exe"], False),
    ("celluloid", "Celluloid", ["celluloid"], [], False),
    ("haruna", "Haruna", ["haruna"], [], False),
    ("totem", "GNOME Videos (Totem)", ["totem"], [], False),
    ("mplayer", "MPlayer", ["mplayer"], [], False),
    ("mpc-hc", "MPC-HC", [], ["{PF}\\MPC-HC\\mpc-hc64.exe", "{PF86}\\MPC-HC\\mpc-hc.exe"], False),
    ("potplayer", "PotPlayer", [], ["{PF}\\DAUM\\PotPlayer\\PotPlayerMini64.exe"], False),
]
TRACKED = {"mpv", "vlc"}   # پلیرهایی که هم شروع از زمان مشخص و هم ثبت موقعیت را پشتیبانی می‌کنند


def detect_players() -> List[dict]:
    found = []
    for pid, name, cmds, paths, tracking in _CATALOG:
        exe = next((w for w in (shutil.which(c) for c in cmds) if w), None)
        if not exe:
            exe = next((p for p in map(_expand, paths) if os.path.isfile(p)), None)
        if exe:
            found.append({"id": pid, "name": name, "path": exe, "tracking": tracking})
    return found


def _spawn(cmd: list) -> subprocess.Popen:
    kw: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True                      # با بسته‌شدن سرور، پلیر بسته نشود
    return subprocess.Popen(cmd, **kw)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Saver:
    """آخرین موقعیت را نگه می‌دارد و هر چند ثانیه یک بار callback را صدا می‌زند."""

    def __init__(self, cb: Callable[[float, Optional[float]], None], every: float = 4.0):
        self.cb, self.every = cb, every
        self.pos: Optional[float] = None
        self.dur: Optional[float] = None
        self.sent: Optional[float] = None
        self.last = 0.0

    def update(self, pos=None, dur=None) -> None:
        if dur is not None and dur > 0:
            self.dur = float(dur)
        if pos is not None and pos >= 2.0:          # ثانیه‌های اول (قبل از پرش به نقطه‌ی شروع) ثبت نمی‌شوند
            self.pos = float(pos)
        if self.pos is not None and self.pos != self.sent and time.monotonic() - self.last >= self.every:
            self.flush()

    def flush(self) -> None:
        if self.pos is not None and self.pos != self.sent:
            self.cb(self.pos, self.dur)
            self.sent, self.last = self.pos, time.monotonic()


# ---------------------------- mpv ----------------------------
def _track_mpv(proc, ipc: str, saver: _Saver) -> None:
    conn = None
    deadline = time.monotonic() + 20
    while conn is None and time.monotonic() < deadline and proc.poll() is None:
        try:
            if os.name == "nt":
                f = open(ipc, "r+b", buffering=0)
                conn = (f.readline, f.write, f.close)
            else:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                try:
                    s.connect(ipc)
                except OSError:
                    s.close()
                    raise
                rf = s.makefile("rb")
                conn = (rf.readline, s.sendall, s.close)
        except OSError:
            time.sleep(0.3)
    if conn is None:
        return
    read, write, close = conn
    try:
        for i, prop in ((1, "time-pos"), (2, "duration")):
            write((json.dumps({"command": ["observe_property", i, prop]}) + "\n").encode())
        while True:
            line = read()
            if not line:
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("event")
            if kind == "property-change" and ev.get("data") is not None:
                if ev.get("name") == "time-pos":
                    saver.update(pos=ev["data"])
                elif ev.get("name") == "duration":
                    saver.update(dur=ev["data"])
            elif kind == "end-file" and ev.get("reason") == "eof" and saver.dur:
                saver.pos = saver.dur                # تا انتها دیده شد
    except (OSError, ValueError):
        pass
    finally:
        try:
            close()
        except OSError:
            pass
        if os.name != "nt":
            try:
                os.unlink(ipc)
            except OSError:
                pass


# ---------------------------- VLC ----------------------------
def _track_vlc(proc, port: int, password: str, saver: _Saver) -> None:
    url = f"http://127.0.0.1:{port}/requests/status.json"
    auth = "Basic " + base64.b64encode((":" + password).encode()).decode()
    t0, seen = time.monotonic(), False
    while proc.poll() is None:
        try:
            req = urllib.request.Request(url, headers={"Authorization": auth})
            with urllib.request.urlopen(req, timeout=2) as resp:
                st = json.loads(resp.read().decode("utf-8", "replace"))
            seen = True
            if st.get("state") in ("playing", "paused"):
                saver.update(pos=st.get("time"), dur=st.get("length"))
        except (OSError, ValueError, http.client.HTTPException):
            if not seen and time.monotonic() - t0 > 30:
                break           # VLC رابط HTTP را بالا نیاورد (مثلاً نمونه‌ی دیگری از VLC باز بود)
        time.sleep(2)


# ---------------------------- باز کردن ----------------------------
def _open_default(video: str) -> None:
    if os.name == "nt":
        os.startfile(video)                                    # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        _spawn(["open", video])
    else:
        _spawn(["xdg-open", video])


def start_player(player: dict, video: str, start: float = 0.0, extra_args: str = "",
                 on_update: Optional[Callable[[float, Optional[float]], None]] = None,
                 on_exit: Optional[Callable[[], None]] = None) -> dict:
    """
    ویدیو را با پلیر داده‌شده باز می‌کند.
    خروجی: {"proc": Popen یا None، "tracking": bool، "can_start": bool}
    """
    pid, exe = player["id"], player.get("path", "")
    start = float(start or 0)
    saver = _Saver(on_update or (lambda *_: None))
    target: Optional[Callable[[], None]] = None

    if pid == "default":
        _open_default(video)
        return {"proc": None, "tracking": False, "can_start": False}

    if pid == "mpv":
        token = secrets.token_hex(4)
        ipc = (r"\\.\pipe\courseplanner-" + token) if os.name == "nt" else f"/tmp/courseplanner-{token}.sock"
        cmd = [exe, f"--input-ipc-server={ipc}"]
        if start >= 1:
            cmd.append(f"--start={int(start)}")
        proc = _spawn(cmd + [video])
        target = lambda: _track_mpv(proc, ipc, saver)          # noqa: E731
    elif pid == "vlc":
        port, password = _free_port(), secrets.token_urlsafe(9)
        cmd = [exe, "--extraintf=http", "--http-host=127.0.0.1", f"--http-port={port}",
               f"--http-password={password}", "--no-one-instance"]
        if start >= 1:
            cmd.append(f"--start-time={int(start)}")
        proc = _spawn(cmd + [video])
        target = lambda: _track_vlc(proc, port, password, saver)   # noqa: E731
    else:
        extra = shlex.split(extra_args, posix=(os.name != "nt")) if pid == "custom" and extra_args else []
        proc = _spawn([exe] + extra + [video])

    if target is not None:
        def run() -> None:
            try:
                target()
            finally:
                saver.flush()
                if on_exit:
                    on_exit()
        threading.Thread(target=run, daemon=True).start()
    else:
        threading.Thread(target=proc.wait, daemon=True).start()    # جلوگیری از پروسه‌ی zombie
    return {"proc": proc, "tracking": target is not None, "can_start": pid in TRACKED}