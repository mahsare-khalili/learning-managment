# /opt/ai/projects/course_web/course_timer.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
course_timer.py - rootend (root + tend: مراقبت از ریشه‌ها)
==========================================================
محاسبه‌ی زمان کل یک دوره‌ی آموزشی ویدیویی + برنامه‌ریزی روزانه + چک‌لیست ترمینالی

استفاده به‌صورت اسکریپت:
    python course_timer.py "/path/to/Course"
    python course_timer.py "/path/to/Course" --daily 2:30
    python course_timer.py "/path/to/Course" --daily 2:30 --grace 20
    python course_timer.py "/path/to/Course" -o my_report.json

استفاده به‌صورت ماژول:
    from course_timer import analyze_course
    report = analyze_course("/path/to/Course", "report.json")
    print(report["total_duration"]["text"])

پیش‌نیاز (یکی از این دو):
    1) ffprobe  (همراه با نصب ffmpeg)           -> پیشنهاد می‌شود
    2) pip install pymediainfo

نیازی به نصب پکیج دیگری نیست (فقط کتابخانه‌ی استاندارد پایتون، نسخه‌ی 3.8 به بالا).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

# پسوندهایی که «ویدیو» حساب می‌شوند. هر چه لازم است اضافه/کم کنید.
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm",
    ".m4v", ".mpg", ".mpeg", ".3gp", ".ogv",
}


# ======================================================================
# توابع کمکی زمان
# ======================================================================
def split_hms(total_seconds: float):
    """ثانیه -> (ساعت، دقیقه، ثانیه) با گردکردن به نزدیک‌ترین ثانیه."""
    total = int(total_seconds + 0.5)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return hours, minutes, seconds


def time_info(ms: int) -> dict:
    """میلی‌ثانیه -> دیکشنری ساعت/دقیقه/ثانیه برای ذخیره در JSON."""
    h, m, s = split_hms(ms / 1000)
    return {
        "hours": h,
        "minutes": m,
        "seconds": s,
        "text": f"{h:02d}:{m:02d}:{s:02d}",
        "total_seconds": round(ms / 1000, 3),
    }


def ms_of(info: Optional[dict]) -> int:
    """برعکس time_info: از دیکشنری زمان، میلی‌ثانیه‌ی دقیق را برمی‌گرداند."""
    if not info:
        return 0
    return int(round(info["total_seconds"] * 1000))


def format_ms(ms: int) -> str:
    return time_info(ms)["text"]


def natural_key(name: str):
    """مرتب‌سازی طبیعی: '2. intro' قبل از '10. basics' می‌آید."""
    parts = re.split(r"(\d+)", name.casefold())
    return [int(p) if i % 2 else p for i, p in enumerate(parts)]


# ======================================================================
# خواندن مدت زمان ویدیو
# ======================================================================
def detect_backend() -> str:
    if shutil.which("ffprobe"):
        return "ffprobe"
    try:
        from pymediainfo import MediaInfo  # type: ignore
        if MediaInfo.can_parse():
            return "pymediainfo"
    except ImportError:
        pass
    raise RuntimeError(
        "Neither ffprobe nor pymediainfo is available.\n"
        "  Install ffmpeg (it includes ffprobe):\n"
        "    Windows: winget install ffmpeg\n"
        "    macOS  : brew install ffmpeg\n"
        "    Linux  : sudo apt install ffmpeg\n"
        "  or install the Python package:  pip install pymediainfo"
    )


def duration_ffprobe(path: str) -> Optional[int]:
    """مدت ویدیو به میلی‌ثانیه (یا None اگر خوانده نشد)."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:stream=duration", path,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None

    def to_num(value):
        try:
            x = float(value)
        except (TypeError, ValueError):
            return None
        return x if math.isfinite(x) and x >= 0 else None

    fmt = to_num((data.get("format") or {}).get("duration"))
    if fmt is not None:
        return int(round(fmt * 1000))
    # بعضی فایل‌ها (مثلاً webm) مدت را فقط در stream دارند
    streams = [to_num(s.get("duration")) for s in data.get("streams", [])]
    streams = [x for x in streams if x is not None]
    return int(round(max(streams) * 1000)) if streams else None


def duration_pymediainfo(path: str) -> Optional[int]:
    try:
        from pymediainfo import MediaInfo  # type: ignore
        info = MediaInfo.parse(path)
        for kind in ("General", "Video"):
            for track in info.tracks:
                if track.track_type == kind and track.duration:
                    return int(round(float(track.duration)))
    except Exception:
        return None
    return None


# ======================================================================
# اسکن دایرکتوری و ساخت درخت
# ======================================================================
class CourseScanner:
    def __init__(self, on_progress: Optional[Callable[[int, str], None]] = None):
        self.backend = detect_backend()
        self.on_progress = on_progress
        self.failed: list = []
        self.root: Optional[Path] = None
        self._count = 0

    def _duration_ms(self, abs_path: str) -> Optional[int]:
        if self.backend == "ffprobe":
            return duration_ffprobe(abs_path)
        return duration_pymediainfo(abs_path)

    def scan(self, directory) -> dict:
        root = Path(directory).expanduser().resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"Not a directory: {root}")
        self.root = root
        result = self._scan_dir(root, root.name or str(root))
        if result is None:
            raise ValueError(f"No video files found in: {root}")
        return result[0]

    def _scan_dir(self, directory: Path, rel: str):
        """(node, total_ms) یا None اگر هیچ ویدیویی در این شاخه نبود."""
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: natural_key(e.name))
        except OSError as exc:
            self.failed.append({"path": rel, "error": str(exc)})
            return None

        videos, subdirs = [], []
        total_ms = 0
        count = 0
        for entry in entries:
            if entry.name.startswith("."):      # فایل‌های مخفی (مثل ._x.mp4 در مک)
                continue
            child_rel = f"{rel}/{entry.name}"
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file()
            except OSError:
                continue

            if is_dir:
                sub = self._scan_dir(Path(entry.path), child_rel)
                if sub:
                    node, ms = sub
                    subdirs.append(node)
                    total_ms += ms
                    count += node["video_count"]
            elif is_file and Path(entry.name).suffix.lower() in VIDEO_EXTENSIONS:
                self._count += 1
                if self.on_progress:
                    self.on_progress(self._count, child_rel)
                ms = self._duration_ms(entry.path)
                if ms is None:
                    self.failed.append({"path": child_rel,
                                        "error": "could not read duration"})
                    videos.append({"name": entry.name, "path": child_rel,
                                   "duration": None})
                else:
                    videos.append({"name": entry.name, "path": child_rel,
                                   "duration": time_info(ms)})
                    total_ms += ms
                count += 1

        if not videos and not subdirs:
            return None
        node = {
            "name": directory.name or str(directory),
            "path": rel,
            "video_count": count,
            "total_duration": time_info(total_ms),
            "videos": videos,
            "subdirectories": subdirs,
        }
        return node, total_ms


def save_json(data: dict, path) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def analyze_course(directory, output_json=None,
                   on_progress: Optional[Callable[[int, str], None]] = None) -> dict:
    """
    دایرکتوری دوره را اسکن می‌کند و گزارش کامل را برمی‌گرداند.
    اگر output_json داده شود، گزارش در آن فایل ذخیره می‌شود.
    """
    scanner = CourseScanner(on_progress)
    tree = scanner.scan(directory)
    report = {
        "course_name": tree["name"],
        "root_directory": str(scanner.root),
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
        "video_count": tree["video_count"],
        "total_duration": tree["total_duration"],
        "failed_files": scanner.failed,
        "tree": tree,
    }
    if output_json:
        save_json(report, output_json)
    return report


def print_tree(node: dict, depth: int = 0) -> None:
    pad = "  " * depth
    print(f"{pad}[{node['name']}]  total {node['total_duration']['text']}"
          f"  ({node['video_count']} videos)")
    for v in node["videos"]:
        d = v["duration"]["text"] if v["duration"] else "??:??:??"
        print(f"{pad}  - {v['name']}   {d}")
    for sub in node["subdirectories"]:
        print_tree(sub, depth + 1)


# ======================================================================
# برنامه‌ریزی روزانه
# ======================================================================
def flatten_videos(node: dict) -> list:
    """همه‌ی ویدیوها به ترتیب درس (اول ویدیوهای همین پوشه، بعد زیرپوشه‌ها)."""
    items = [{"path": v["path"], "name": v["name"], "ms": ms_of(v["duration"])}
             for v in node["videos"]]
    for sub in node["subdirectories"]:
        items.extend(flatten_videos(sub))
    return items


def build_schedule(videos: list, daily_minutes: int, grace_minutes: int = 0) -> list:
    """
    ویدیوها را پشت سر هم در روزها می‌چیند. ویدیو بین دو روز نصف نمی‌شود:
    اگر در جای باقی‌مانده‌ی امروز جا نشد، به فردا می‌رود.
    grace_minutes: هر روز می‌تواند تا این مقدار (دقیقه) از زمان روزانه بیشتر شود
    تا ویدیوی بعدی هنوز در همان روز جا شود (تجاوز مجاز).
    ویدیویی که از سقف یک روز هم بلندتر باشد، یک روز تنها می‌گیرد.
    """
    limit_ms = (daily_minutes + max(grace_minutes, 0)) * 60_000
    days, current, used = [], [], 0
    for v in videos:
        if current and used + v["ms"] > limit_ms:
            days.append(current)
            current, used = [], 0
        current.append(v)
        used += v["ms"]
    if current:
        days.append(current)
    return days


def schedule_stats(schedule: list, daily_minutes: int) -> dict:
    """تعداد روز، بلندترین روز و تعداد روزهایی که از زمان روزانه بیشتر شده‌اند."""
    daily_ms = daily_minutes * 60_000
    day_ms = [sum(v["ms"] for v in day) for day in schedule]
    return {
        "planned_days": len(schedule),
        "longest_day": time_info(max(day_ms, default=0)),
        "overrun_days": sum(m > daily_ms for m in day_ms),
    }


def apply_progress(plan: dict, completed) -> None:
    """
    وضعیت «انجام‌شده» را در فایل پلن هماهنگ می‌کند:
      - plan["completed"]  : فهرست مسیر ویدیوهای تمام‌شده (به ترتیب درس)
      - هر ویدیو           : done
      - هر روز             : done، done_count، video_count
    هم چک‌لیست ترمینال و هم برنامه‌ی وب از همین تابع استفاده می‌کنند.
    """
    completed = set(completed)
    ordered = []
    for day in plan["days"]:
        n_done = 0
        for v in day["videos"]:
            v["done"] = v["path"] in completed
            if v["done"]:
                n_done += 1
                ordered.append(v["path"])
        day["video_count"] = len(day["videos"])
        day["done_count"] = n_done
        day["done"] = n_done == len(day["videos"])
    plan["completed"] = ordered
    plan["updated_at"] = datetime.now().isoformat(timespec="seconds")


def make_plan(report: dict, daily_minutes: int, grace_minutes: int = 0) -> dict:
    videos = flatten_videos(report["tree"])
    total_ms = sum(v["ms"] for v in videos)
    daily_ms = daily_minutes * 60_000
    schedule = build_schedule(videos, daily_minutes, grace_minutes)
    stats = schedule_stats(schedule, daily_minutes)
    plan = {
        "course_name": report["course_name"],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "daily_minutes": daily_minutes,
        "daily_time": f"{daily_minutes // 60:02d}:{daily_minutes % 60:02d}",
        "grace_minutes": grace_minutes,
        "total_duration": time_info(total_ms),
        "min_days_if_split_anywhere": -(-total_ms // daily_ms),
        "planned_days": stats["planned_days"],
        "longest_day": stats["longest_day"],
        "overrun_days": stats["overrun_days"],
        "days": [
            {
                "day": i,
                "done": False,
                "done_count": 0,
                "video_count": len(day),
                "total_duration": time_info(sum(v["ms"] for v in day)),
                "videos": [{"path": v["path"], "name": v["name"],
                            "duration": time_info(v["ms"]), "done": False}
                           for v in day],
            }
            for i, day in enumerate(schedule, 1)
        ],
        "completed": [],
    }
    apply_progress(plan, [])
    return plan


def load_matching_plan(plan_path, videos: list) -> Optional[dict]:
    """پلن ذخیره‌شده را فقط اگر دقیقاً برای همین مجموعه ویدیو باشد برمی‌گرداند."""
    try:
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)
        saved = [v["path"] for d in plan["days"] for v in d["videos"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return plan if saved == [v["path"] for v in videos] else None


def print_plan_summary(plan: dict, videos: list) -> None:
    grace = plan.get("grace_minutes", 0)
    limit_ms = (plan["daily_minutes"] + grace) * 60_000
    print()
    print(f"Course length    : {plan['total_duration']['text']}  ({len(videos)} videos)")
    print(f"Daily study time : {plan['daily_time']}")
    print(f"Minimum days     : {plan['min_days_if_split_anywhere']}"
          "   (if you could stop in the middle of a video)")
    print(f"Planned days     : {plan['planned_days']}"
          "   (whole videos only, no splitting across days)")
    if grace:
        longest = (plan.get("longest_day") or {}).get("text", "--:--:--")
        print(f"Daily grace      : {grace} min   (a day may run up to "
              f"{format_ms(limit_ms)} so videos are not split)")
        print(f"Longest day      : {longest}   "
              f"({plan.get('overrun_days', 0)} day(s) exceed the daily time)")
    too_long = sum(v["ms"] > limit_ms for v in videos)
    if too_long:
        print(f"Note: {too_long} video(s) are longer than your daily time "
              "and get a day of their own.")
    print()


# ======================================================================
# ورودی‌گرفتن از کاربر
# ======================================================================
def ask_yes_no(question: str, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = input(f"{question} {suffix} ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


def ask_int(label: str, lo: int, hi: int, default: int = 0) -> int:
    while True:
        raw = input(f"{label} ({lo}-{hi}, default {default}): ").strip()
        if not raw:
            return default
        if raw.isdigit() and lo <= int(raw) <= hi:
            return int(raw)
        print(f"  Please enter a whole number between {lo} and {hi}.")


def ask_daily_minutes() -> int:
    print("How much time can you study per day?")
    while True:
        hours = ask_int("  Hours  ", 0, 23)
        minutes = ask_int("  Minutes", 0, 59)
        if hours * 60 + minutes > 0:
            return hours * 60 + minutes
        print("  Please enter a time greater than zero.")


def parse_grace(text: str) -> int:
    """'20' -> 20 (دقیقه). برای گزینه‌ی --grace."""
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= 120:
        raise argparse.ArgumentTypeError("use a whole number of minutes, 0-120")
    return value


def parse_hhmm(text: str) -> int:
    """'2:30' -> 150 (دقیقه). برای گزینه‌ی --daily."""
    try:
        h, _, m = text.partition(":")
        hours, minutes = int(h), int(m or 0)
        if hours < 0 or not 0 <= minutes < 60 or hours * 60 + minutes == 0:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("use H:MM, for example 2:30") from None
    return hours * 60 + minutes


# ======================================================================
# چک‌لیست ترمینالی
# ======================================================================
def clear_screen() -> None:
    if sys.stdout.isatty():
        os.system("cls" if os.name == "nt" else "clear")


HELP_LINE = ("Commands: <n> or <n-m> toggle videos | d<N> toggle a whole day | "
             "s<N> view day N | c current day | a all days | q save & quit")


def run_checklist(plan: dict, plan_path) -> None:
    # شماره‌گذاری سراسری ویدیوها: 1..N به ترتیب روزها
    day_items: dict = {}
    items: list = []
    n = 0
    for d in plan["days"]:
        lst = []
        for v in d["videos"]:
            n += 1
            it = {"n": n, "path": v["path"], "ms": ms_of(v["duration"])}
            lst.append(it)
            items.append(it)
        day_items[d["day"]] = lst
    by_number = {it["n"]: it for it in items}
    total_videos = len(items)
    total_ms = sum(it["ms"] for it in items)
    width = len(str(total_videos))
    day_width = len(str(len(day_items)))

    done = set(plan.get("completed", [])) & {it["path"] for it in items}
    view_day: Optional[int] = None
    show_all = False

    def day_done(day_no: int) -> bool:
        return all(it["path"] in done for it in day_items[day_no])

    def first_open_day() -> Optional[int]:
        return next((d for d in day_items if not day_done(d)), None)

    def save() -> None:
        apply_progress(plan, done)
        save_json(plan, plan_path)

    def render() -> None:
        clear_screen()
        watched = sum(it["ms"] for it in items if it["path"] in done)
        pct = (100 * watched / total_ms) if total_ms else \
              (100 * len(done) / max(total_videos, 1))
        days_done = sum(day_done(d) for d in day_items)
        print(f"=== {plan['course_name']} - checklist ===")
        print(f"Videos: {len(done)}/{total_videos}    "
              f"Days: {days_done}/{len(day_items)}    "
              f"Daily time: {plan['daily_time']}")
        print(f"Watched: {format_ms(watched)} / {format_ms(total_ms)} "
              f"({pct:.1f}%)    Remaining: {format_ms(total_ms - watched)}")
        print()
        for d, lst in day_items.items():
            mark = "x" if day_done(d) else " "
            cnt = sum(it["path"] in done for it in lst)
            dur = format_ms(sum(it["ms"] for it in lst))
            print(f"[{mark}] Day {d:>{day_width}}   {cnt}/{len(lst)} videos   {dur}")
        print()

        if show_all:
            shown = list(day_items)
        else:
            shown = [view_day if view_day is not None else first_open_day()]
        for d in shown:
            if d is None:
                print("All days are complete. Well done!")
                print()
                continue
            lst = day_items[d]
            print(f"--- Day {d}  ({format_ms(sum(it['ms'] for it in lst))}) ---")
            for it in lst:
                mark = "x" if it["path"] in done else " "
                print(f" [{mark}] {it['n']:>{width}}. {it['path']}   {format_ms(it['ms'])}")
            print()
        print(HELP_LINE)

    message = ""
    while True:
        render()
        if message:
            print(message)
            message = ""
        try:
            raw = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break

        quit_now = False
        problems = []
        for tok in re.split(r"[,\s]+", raw.strip().lower()):
            if not tok:
                continue
            if tok in ("q", "quit", "exit"):
                quit_now = True
                break
            if tok in ("a", "all"):
                show_all = not show_all
                continue
            if tok in ("c", "current"):
                view_day, show_all = None, False
                continue
            m = re.fullmatch(r"s(\d+)", tok)
            if m:
                d = int(m.group(1))
                if d in day_items:
                    view_day, show_all = d, False
                else:
                    problems.append(f"no such day: {d}")
                continue
            m = re.fullmatch(r"(d?)(\d+)(?:-(\d+))?", tok)
            if m:
                is_day = m.group(1) == "d"
                lo = int(m.group(2))
                hi = int(m.group(3) or m.group(2))
                if lo > hi:
                    lo, hi = hi, lo
                limit = len(day_items) if is_day else total_videos
                if lo < 1 or hi > limit:
                    problems.append(f"out of range: {tok} (1-{limit})")
                    continue
                if is_day:
                    selected = [it for dn in range(lo, hi + 1) for it in day_items[dn]]
                else:
                    selected = [by_number[k] for k in range(lo, hi + 1)]
                paths = {it["path"] for it in selected}
                if paths <= done:       # همه تیک خورده‌اند -> تیک را بردار
                    done -= paths
                else:                   # وگرنه همه را تیک بزن
                    done |= paths
                continue
            problems.append(f"unknown command: {tok}")

        save()
        if problems:
            message = "! " + "; ".join(problems)
        if quit_now:
            break

    print(f"Progress saved to: {plan_path}")


def run_planner(report: dict, plan_path, daily_minutes: Optional[int] = None,
                grace_minutes: Optional[int] = None) -> None:
    videos = flatten_videos(report["tree"])

    saved = load_matching_plan(plan_path, videos)
    same_settings = bool(saved) and (
        (daily_minutes is None or daily_minutes == saved["daily_minutes"]) and
        (grace_minutes is None or grace_minutes == saved.get("grace_minutes", 0)))
    if saved and same_settings:
        q = (f"A saved plan exists ({saved['daily_time']} per day, "
             f"{len(saved.get('completed', []))}/{len(videos)} videos done). "
             "Continue it? (No = new plan, progress resets)")
        if ask_yes_no(q, default=True):
            run_checklist(saved, plan_path)
            return

    if daily_minutes is None:
        daily_minutes = ask_daily_minutes()
    plan = make_plan(report, daily_minutes, grace_minutes or 0)
    save_json(plan, plan_path)
    print_plan_summary(plan, videos)

    if ask_yes_no("Open the checklist now?", default=True):
        run_checklist(plan, plan_path)
    else:
        print(f"Plan saved to: {plan_path}")
        print("Run the same command again to continue with the checklist.")


# ======================================================================
# اجرای مستقیم
# ======================================================================
def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "course"


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")   # جلوگیری از خطای چاپ در ترمینال‌های قدیمی
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="rootend - calculate the total duration of a video course, "
                    "plan study days and track progress with a checklist.")
    parser.add_argument("directory", nargs="?", help="course directory")
    parser.add_argument("-o", "--output",
                        help="JSON report path (default: <course>_report.json "
                             "in the current directory)")
    parser.add_argument("--daily", type=parse_hhmm, metavar="H:MM",
                        help="daily study time, e.g. 2:30 (skips the question)")
    parser.add_argument("--grace", type=parse_grace, metavar="MIN",
                        help="extra minutes a day may exceed the daily time so "
                             "videos are not split (default 0)")
    args = parser.parse_args(argv)

    try:
        directory = args.directory or input("Course directory: ").strip().strip("\"'")

        def progress(n: int, rel: str) -> None:
            print("\r" + f"  [{n}] {rel}"[:78].ljust(78), end="", flush=True)

        print("Scanning videos...")
        report = analyze_course(directory, on_progress=progress)
        print("\r" + " " * 78 + "\r", end="")

        safe = safe_filename(report["course_name"])
        report_path = Path(args.output) if args.output else Path.cwd() / f"{safe}_report.json"
        plan_path = report_path.with_name(f"{safe}_plan.json")
        save_json(report, report_path)

        print_tree(report["tree"])
        t = report["total_duration"]
        print()
        print(f"Total: {report['video_count']} videos, "
              f"{t['hours']} hours, {t['minutes']} minutes, {t['seconds']} seconds "
              f"({t['text']})")
        print(f"JSON report saved to: {report_path}")
        for item in report["failed_files"]:
            print(f"WARNING: {item['path']} -> {item['error']}")

        if t["total_seconds"] <= 0:
            return 0
        if args.daily or ask_yes_no(
                "\nDo you want to calculate how many days it takes to finish the course?",
                default=True):
            run_planner(report, plan_path, args.daily, args.grace)
        return 0

    except (RuntimeError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())