# /opt/ai/projects/rootend/app.py

# -*- coding: utf-8 -*-
"""
app.py - rootend (root + tend: مراقبت از ریشه‌ها)، برنامه‌ی وب FastAPI برای برنامه‌ریزی و پیگیری دوره‌های آموزشی

هر «برنامه» (program) یک اسم دارد و به یک پوشه‌ی دوره وصل است. سه فایل JSON در پوشه‌ی data/:
    <id>_meta.json     نام برنامه، مسیر پوشه، تاریخ ساخت/ویرایش
    <id>_report.json   گزارش اسکن (درخت پوشه‌ها و زمان‌ها)
    <id>_plan.json     برنامه‌ی روزانه + تجاوز مجاز + وضعیت تیک‌ها

فایل‌های قدیمی (<نام>_report.json و <نام>_plan.json بدون meta) خودکار به‌عنوان برنامه شناخته می‌شوند؛
با اولین ویرایش، فایل meta برایشان ساخته می‌شود. چیزی پاک یا جابه‌جا نمی‌شود.

برنامه‌ی حذف‌شده به data/trash/ منتقل می‌شود (قابل بازگردانی)، نه این‌که برای همیشه پاک شود.
<id>_positions.json زمان توقف ویدیوها را نگه می‌دارد.
<id>_events.jsonl لاگ فقط-افزودنی رویدادها (هر خط یک JSON): plan_changed، video_done، play_session. تنظیمات پلیر در data/settings.json است.
کارهای وابسته به سیستم‌عامل (انتخاب پوشه، پلیر) در system_tools.py هستند.

اجرا:
    python app.py
سپس در مرورگر:  http://127.0.0.1:18000
"""
import json
import os
import shutil
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import course_timer as ct
import system_tools as st

BASE_DIR = Path(__file__).resolve().parent
# اگر فایل‌های JSON شما جای دیگری است، می‌توانید با متغیر محیطی ROOTEND_DATA_DIR مسیرش را بدهید
DATA_DIR = Path(os.environ.get("ROOTEND_DATA_DIR") or os.environ.get("COURSE_DATA_DIR") or BASE_DIR / "data").expanduser().resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
TRASH_DIR = DATA_DIR / "trash"
INDEX_HTML = BASE_DIR / "static" / "index.html"

KINDS = ("meta", "report", "plan", "positions")
SETTINGS_FILE = DATA_DIR / "settings.json"
MAX_NAME = 80
MAX_GRACE = 120   # بیشترین تجاوز مجاز در روز (دقیقه)

app = FastAPI(title="rootend")

# لوگو و تصویرهای ثابت: assets/images/... از آدرس /assets/images/... در دسترس است
ASSETS_DIR = BASE_DIR / "assets"
if ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")

# چون برنامه می‌تواند پلیر باز کند و پوشه‌ها را فهرست کند، فقط درخواست‌های محلی پذیرفته می‌شود
# (جلوگیری از سوءاستفاده‌ی صفحه‌های دیگر مرورگر و DNS rebinding).
# اگر برنامه را با نام دیگری باز می‌کنید: ROOTEND_ALLOWED_HOSTS=my-host,other-host
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"} | {
    h.strip().lower() for h in (os.environ.get("ROOTEND_ALLOWED_HOSTS") or os.environ.get("COURSE_ALLOWED_HOSTS") or "").split(",") if h.strip()}


@app.middleware("http")
async def local_only(request, call_next):
    host_header = request.headers.get("host", "")
    host = (urlparse("//" + host_header).hostname or "").lower()
    origin = request.headers.get("origin")
    if host not in ALLOWED_HOSTS or (origin and urlparse(origin).netloc.lower() != host_header.lower()):
        return JSONResponse({"detail": "این درخواست از مبدأ مجاز نیست."}, status_code=403)
    return await call_next(request)

# چند درخواست هم‌زمان نباید هم‌زمان در یک فایل بنویسند
write_lock = threading.RLock()


# ----------------------------------------------------------------------
# مدل‌های ورودی
# ----------------------------------------------------------------------
class ProgramCreate(BaseModel):
    name: str = ""            # خالی = نام پوشه
    directory: str


class ProgramPatch(BaseModel):
    name: Optional[str] = None
    directory: Optional[str] = None
    rescan: bool = False


class PlanRequest(BaseModel):
    hours: int = Field(0, ge=0, le=23)
    minutes: int = Field(0, ge=0, le=59)
    grace_minutes: int = Field(0, ge=0, le=MAX_GRACE)
    keep_progress: bool = False


class ProgressRequest(BaseModel):
    paths: List[str]
    done: bool
    source: Literal["video_checkbox", "day_checkbox"] = "video_checkbox"


# ----------------------------------------------------------------------
# کمک‌ها: فایل‌ها
# ----------------------------------------------------------------------
def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def check_id(cid: str) -> str:
    """جلوگیری از مسیرهای مخرب مثل ../"""
    if not cid or cid in (".", "..") or cid != ct.safe_filename(cid):
        raise HTTPException(404, "برنامه پیدا نشد.")
    return cid


def course_file(cid: str, kind: str) -> Path:
    if kind not in KINDS:
        raise HTTPException(404, "نوع فایل نامعتبر است.")
    return DATA_DIR / f"{check_id(cid)}_{kind}.json"


def read_json(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise HTTPException(404, f"فایل پیدا نشد: {path.name}")
    except ValueError:
        raise HTTPException(500, f"فایل JSON خراب است: {path.name}")


def read_optional(path: Path) -> Optional[dict]:
    try:
        return read_json(path)
    except HTTPException:
        return None


# ----------------------------------------------------------------------
# لاگ رویدادها: <id>_events.jsonl (فقط افزودنی؛ هر خط یک رویداد)
# ----------------------------------------------------------------------
events_lock = threading.Lock()


def events_file(cid: str) -> Path:
    return DATA_DIR / f"{check_id(cid)}_events.jsonl"


def log_event(cid: str, etype: str, ts: Optional[str] = None, **fields) -> None:
    """یک خط به لاگ اضافه می‌کند. ثبت لاگ هرگز نباید کار اصلی کاربر را خراب کند."""
    rec = {"v": 1, "ts": ts or ct.now_stamp(), "type": etype, **fields}
    try:
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with events_lock:
            with open(events_file(cid), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except (OSError, TypeError, ValueError, HTTPException) as exc:
        print(f"rootend: could not write the event log: {exc}", file=sys.stderr)


def read_events(cid: str) -> List[dict]:
    out: List[dict] = []
    try:
        with open(events_file(cid), encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue                     # خط ناقص (مثلاً قطع شدن برق) را رد کن
                if isinstance(rec, dict):
                    out.append(rec)
    except (OSError, HTTPException):
        pass
    return out


def existing_ids() -> List[str]:
    ids = set()
    for kind in KINDS:
        suffix = f"_{kind}.json"
        for p in DATA_DIR.glob(f"*{suffix}"):
            ids.add(p.name[: -len(suffix)])
    return sorted((i for i in ids if i and i == ct.safe_filename(i)), key=ct.natural_key)


def new_id() -> str:
    while True:
        cid = "p" + uuid.uuid4().hex[:8]
        if not any(course_file(cid, k).exists() for k in KINDS) and not (TRASH_DIR / cid).exists():
            return cid


def load_program(cid: str):
    meta = read_optional(course_file(cid, "meta")) or {}
    report = read_optional(course_file(cid, "report"))
    plan = read_optional(course_file(cid, "plan"))
    if not (meta or report or plan):
        raise HTTPException(404, "برنامه پیدا نشد.")
    return meta, report, plan


# ----------------------------------------------------------------------
# کمک‌ها: نام و مسیر
# ----------------------------------------------------------------------
def clean_name(name: Optional[str]) -> str:
    name = " ".join((name or "").split())
    if len(name) > MAX_NAME:
        raise HTTPException(400, f"نام برنامه حداکثر {MAX_NAME} نویسه می‌تواند باشد.")
    return name


def clean_dir(directory: Optional[str]) -> str:
    return (directory or "").strip().strip("\"'").strip()


def norm_path(p: str) -> str:
    try:
        return os.path.normcase(str(Path(p).expanduser().resolve()))
    except (OSError, RuntimeError):
        return p


def name_of(cid: str) -> str:
    meta = read_optional(course_file(cid, "meta")) or {}
    if meta.get("name"):
        return meta["name"]
    src = read_optional(course_file(cid, "report")) or read_optional(course_file(cid, "plan")) or {}
    return src.get("course_name", cid)


def name_taken(name: str, exclude: Optional[str] = None) -> bool:
    key = name.casefold()
    return any(name_of(i).casefold() == key for i in existing_ids() if i != exclude)


def ensure_name_free(name: str, exclude: Optional[str] = None) -> None:
    if name_taken(name, exclude):
        raise HTTPException(409, f"برنامه‌ای با نام «{name}» از قبل وجود دارد. نام دیگری انتخاب کنید.")


def unique_name(base: str) -> str:
    name, n = base, 2
    while name_taken(name):
        name = f"{base} ({n})"
        n += 1
    return name


def scan_directory(directory: str) -> dict:
    if not directory:
        raise HTTPException(400, "مسیر پوشه را وارد کنید.")
    try:
        return ct.analyze_course(directory)
    except NotADirectoryError:
        raise HTTPException(400, f"این مسیر وجود ندارد یا پوشه نیست: {directory}")
    except ValueError:
        raise HTTPException(400, "در این پوشه (و زیرپوشه‌هایش) هیچ ویدیویی پیدا نشد.")
    except RuntimeError as exc:   # نه ffprobe هست نه pymediainfo
        raise HTTPException(500, f"ابزار خواندن مدت ویدیو پیدا نشد. {exc}")
    except OSError as exc:
        raise HTTPException(400, f"خطا در خواندن پوشه: {exc}")


# ----------------------------------------------------------------------
# کمک‌ها: پلن و پیشرفت
# ----------------------------------------------------------------------
def plan_video_count(plan: dict) -> int:
    return sum(len(d.get("videos", [])) for d in plan.get("days", []))


def plan_matches_report(report: Optional[dict], plan: Optional[dict]) -> Optional[bool]:
    """آیا پلن دقیقاً برای همین مجموعه ویدیوهای گزارش ساخته شده؟"""
    if not report or not plan:
        return None
    try:
        want = [v["path"] for v in ct.flatten_videos(report["tree"])]
        have = [v["path"] for d in plan["days"] for v in d["videos"]]
    except (KeyError, TypeError):
        return False
    return want == have


def plan_progress(plan: dict) -> dict:
    done = set(plan.get("completed", []))
    total_ms = watched_ms = n_videos = n_done = days_done = 0
    current = None
    days = plan.get("days", [])
    for d in days:
        all_done = True
        for v in d["videos"]:
            ms = ct.ms_of(v.get("duration"))
            total_ms += ms
            n_videos += 1
            if v["path"] in done:
                watched_ms += ms
                n_done += 1
            else:
                all_done = False
        if all_done:
            days_done += 1
        elif current is None:
            current = d["day"]
    if total_ms:
        pct = 100 * watched_ms / total_ms
    else:
        pct = 100 * n_done / n_videos if n_videos else 0
    return {
        "done_videos": n_done, "total_videos": n_videos,
        "done_days": days_done, "total_days": len(days),
        "current_day": current, "progress_pct": round(pct, 1),
    }


def plan_brief(plan: dict) -> dict:
    return {
        "daily_minutes": plan.get("daily_minutes", 0),
        "daily_time": plan.get("daily_time", ""),
        "grace_minutes": plan.get("grace_minutes", 0),
        **plan_progress(plan),
    }


def summarize(cid: str, meta: dict, report: Optional[dict], plan: Optional[dict]) -> dict:
    src = report or plan or {}
    directory = meta.get("directory") or (report or {}).get("root_directory", "")
    try:
        exists = bool(directory) and Path(directory).is_dir()
    except OSError:
        exists = False
    try:
        brief = plan_brief(plan) if plan else None
    except (KeyError, TypeError):
        brief = None
    stamps = [x for x in (meta.get("created_at"), meta.get("updated_at"),
                          (plan or {}).get("updated_at"), (report or {}).get("scanned_at")) if x]
    if report:
        video_count = report["video_count"]
    else:
        video_count = plan_video_count(plan) if plan else 0
    return {
        "id": cid,
        "name": meta.get("name") or src.get("course_name", cid),
        "directory": directory,
        "directory_exists": exists,
        "created_at": meta.get("created_at") or (report or {}).get("scanned_at"),
        "last_activity": max(stamps) if stamps else "",
        "video_count": video_count,
        "total_duration": src.get("total_duration"),
        "has_report": report is not None,
        "has_plan": plan is not None,
        "plan_matches_report": plan_matches_report(report, plan),
        "plan": brief,
    }


def _tail(path: str) -> str:
    """مسیر ویدیو بدون نام پوشه‌ی ریشه ('Course/a/b.mp4' -> 'a/b.mp4')."""
    return path.split("/", 1)[1] if "/" in path else path


def remap_completed(old_completed: List[str], plan: dict) -> List[str]:
    """
    تیک‌های قبلی را روی ویدیوهای پلن جدید می‌گذارد. اگر نام پوشه‌ی ریشه عوض شده باشد
    (مثلاً مسیر دوره جابه‌جا شده) هم تیک‌ها با مسیر نسبی داخل دوره حفظ می‌شوند.
    """
    exact = set(old_completed)
    tails = {_tail(p) for p in old_completed}
    return [v["path"] for d in plan["days"] for v in d["videos"]
            if v["path"] in exact or _tail(v["path"]) in tails]


def remap_done_at(old_plan: dict, plan: dict) -> dict:
    """زمان تیک‌خوردن ویدیوها (done_at) را از پلن قبلی به پلن جدید می‌برد؛ با همان مسیر داخل دوره."""
    stamps = {_tail(v["path"]): v["done_at"]
              for d in old_plan.get("days", []) for v in d.get("videos", []) if v.get("done_at")}
    return {v["path"]: stamps[_tail(v["path"])]
            for d in plan["days"] for v in d["videos"] if _tail(v["path"]) in stamps}


def plan_settings(plan: Optional[dict]) -> Optional[dict]:
    if not plan:
        return None
    return {"daily_minutes": plan.get("daily_minutes"), "grace_minutes": plan.get("grace_minutes", 0),
            "planned_days": plan.get("planned_days")}


# ----------------------------------------------------------------------
# API: برنامه‌ها
# ----------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(INDEX_HTML)


@app.get("/api/programs")
def list_programs():
    """همه‌ی برنامه‌ها؛ تازه‌ترین فعالیت اول."""
    items = []
    for cid in existing_ids():
        meta = read_optional(course_file(cid, "meta")) or {}
        report = read_optional(course_file(cid, "report"))
        plan = read_optional(course_file(cid, "plan"))
        if not (meta or report or plan):
            continue
        items.append(summarize(cid, meta, report, plan))
    items.sort(key=lambda p: ct.natural_key(p["name"]))
    items.sort(key=lambda p: p["last_activity"], reverse=True)   # sort پایدار است
    return items


@app.post("/api/programs")
def create_program(req: ProgramCreate):
    """پوشه را اسکن می‌کند و یک برنامه‌ی جدید می‌سازد (بدون برنامه‌ی روزانه)."""
    directory = clean_dir(req.directory)
    if not directory:
        raise HTTPException(400, "مسیر پوشه را وارد کنید.")
    name = clean_name(req.name)
    if name:
        ensure_name_free(name)          # قبل از اسکنِ طولانی، زود خطا بدهد
    report = scan_directory(directory)

    with write_lock:
        if name:
            ensure_name_free(name)
        else:
            name = unique_name(report["course_name"][:MAX_NAME])
        cid = new_id()
        stamp = now()
        ct.save_json({"name": name, "directory": report["root_directory"],
                      "created_at": stamp, "updated_at": stamp}, course_file(cid, "meta"))
        ct.save_json(report, course_file(cid, "report"))
    return {"id": cid, "name": name, "video_count": report["video_count"],
            "failed": len(report["failed_files"])}


@app.get("/api/programs/{cid}")
def get_program(cid: str):
    meta, report, plan = load_program(cid)
    return {
        "program": summarize(cid, meta, report, plan),
        "report": report,
        "plan": plan,
        "plan_matches_report": plan_matches_report(report, plan),
        "positions": read_optional(course_file(cid, "positions")) or {},
        "active": active_tails(cid),
    }


@app.patch("/api/programs/{cid}")
def update_program(cid: str, req: ProgramPatch):
    """تغییر نام و/یا مسیر پوشه، یا اسکن مجدد. با تغییر مسیر، پوشه دوباره اسکن می‌شود."""
    meta, report, plan = load_program(cid)
    current = summarize(cid, meta, report, plan)

    name = None
    if req.name is not None:
        name = clean_name(req.name)
        if not name:
            raise HTTPException(400, "نام برنامه نمی‌تواند خالی باشد.")
        if name != current["name"]:
            ensure_name_free(name, exclude=cid)

    directory = None
    if req.directory is not None:
        directory = clean_dir(req.directory)
        if not directory:
            raise HTTPException(400, "مسیر پوشه را وارد کنید.")
    dir_changed = directory is not None and (
        not current["directory"] or norm_path(directory) != norm_path(current["directory"]))

    new_report = None
    if dir_changed or req.rescan:
        new_report = scan_directory(directory if dir_changed else current["directory"])

    with write_lock:
        meta = read_optional(course_file(cid, "meta")) or {}
        meta.setdefault("created_at", current["created_at"] or now())
        meta["name"] = name if name is not None else meta.get("name") or current["name"]
        if new_report is not None:
            meta["directory"] = new_report["root_directory"]
            ct.save_json(new_report, course_file(cid, "report"))
            report = new_report
        else:
            meta.setdefault("directory", current["directory"])
        meta["updated_at"] = now()
        ct.save_json(meta, course_file(cid, "meta"))

    summary = summarize(cid, meta, report, plan)
    return {
        "program": summary,
        "rescanned": new_report is not None,
        "video_count": summary["video_count"],
        "failed": len(new_report["failed_files"]) if new_report else 0,
    }


@app.delete("/api/programs/{cid}")
def delete_program(cid: str):
    """برنامه را (با گزارش و تیک‌هایش) به data/trash/ می‌برد؛ ویدیوهای روی دیسک دست‌نخورده می‌مانند."""
    with write_lock:
        files = [p for p in (course_file(cid, k) for k in KINDS) if p.exists()]
        if not files:
            raise HTTPException(404, "برنامه پیدا نشد.")
        if events_file(cid).exists():
            files.append(events_file(cid))
        dest = TRASH_DIR / cid
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        for p in files:
            os.replace(p, dest / p.name)
    return {"deleted": cid}


@app.post("/api/programs/{cid}/restore")
def restore_program(cid: str):
    check_id(cid)
    src = TRASH_DIR / cid
    with write_lock:
        if not src.is_dir():
            raise HTTPException(404, "چیزی برای بازگردانی پیدا نشد.")
        if any(course_file(cid, k).exists() for k in KINDS):
            raise HTTPException(409, "این برنامه دوباره وجود دارد.")
        for p in list(src.iterdir()):
            os.replace(p, DATA_DIR / p.name)
        try:
            src.rmdir()
        except OSError:
            pass
    return {"restored": cid}


# ----------------------------------------------------------------------
# API: برنامه‌ی روزانه و پیشرفت
# ----------------------------------------------------------------------
@app.get("/api/programs/{cid}/preview")
def preview_plan(cid: str,
                 hours: int = Query(0, ge=0, le=23),
                 minutes: int = Query(0, ge=0, le=59),
                 grace: int = Query(0, ge=0, le=MAX_GRACE)):
    """پیش‌نمایش بدون ذخیره: با این زمان روزانه و تجاوز مجاز، برنامه چند روز می‌شود؟"""
    daily = hours * 60 + minutes
    if daily <= 0:
        raise HTTPException(400, "زمان روزانه باید بیشتر از صفر باشد.")
    report = read_json(course_file(cid, "report"))
    videos = ct.flatten_videos(report["tree"])
    total_ms = sum(v["ms"] for v in videos)
    stats = ct.schedule_stats(ct.build_schedule(videos, daily, grace), daily)
    stats["days_without_grace"] = (len(ct.build_schedule(videos, daily, 0))
                                   if grace else stats["planned_days"])
    stats["min_days"] = -(-total_ms // (daily * 60_000))
    return stats


@app.post("/api/programs/{cid}/plan")
def create_plan(cid: str, req: PlanRequest):
    """برنامه‌ی روزانه می‌سازد (و جایگزین پلن قبلی می‌کند)."""
    daily_minutes = req.hours * 60 + req.minutes
    if daily_minutes <= 0:
        raise HTTPException(400, "زمان روزانه باید بیشتر از صفر باشد.")

    with write_lock:
        report = read_json(course_file(cid, "report"))
        if report["total_duration"]["total_seconds"] <= 0:
            raise HTTPException(400, "مدت کل ویدیوها صفر است؛ برنامه‌ریزی ممکن نیست.")
        plan = ct.make_plan(report, daily_minutes, req.grace_minutes)

        plan_path = course_file(cid, "plan")
        old = read_optional(plan_path)
        if req.keep_progress and old:
            ct.apply_progress(plan, remap_completed(old.get("completed", []), plan),
                              remap_done_at(old, plan))
        ct.save_json(plan, plan_path)

        before, after = plan_settings(old), plan_settings(plan)
        if not old:
            action = "created"
        elif (before["daily_minutes"], before["grace_minutes"]) == (after["daily_minutes"], after["grace_minutes"]):
            action = "rebuilt"            # همان تنظیمات (مثلاً بعد از اسکن مجدد)
        else:
            action = "changed"
        log_event(cid, "plan_changed", action=action, before=before, after=after,
                  keep_progress=req.keep_progress,
                  done_before=len(old.get("completed", [])) if old else 0,
                  done_after=len(plan["completed"]))
    return plan


@app.post("/api/programs/{cid}/progress")
def set_progress(cid: str, req: ProgressRequest):
    """تیک‌زدن / برداشتن تیک چند ویدیو؛ بلافاصله در فایل پلن ذخیره می‌شود."""
    path = course_file(cid, "plan")
    with write_lock:
        plan = read_json(path)
        valid = {v["path"] for d in plan["days"] for v in d["videos"]}
        unknown = [p for p in req.paths if p not in valid]
        if unknown:
            raise HTTPException(400, f"ویدیوی ناشناخته در این پلن: {unknown[0]}")

        before = set(plan.get("completed", []))
        completed = set(before)
        if req.done:
            completed |= set(req.paths)
        else:
            completed -= set(req.paths)
        changed = [p for p in dict.fromkeys(req.paths) if (p in completed) != (p in before)]
        stamp = ct.now_stamp()
        ct.apply_progress(plan, completed, {p: stamp for p in changed} if req.done else None)
        ct.save_json(plan, path)

        day_of = {v["path"]: d["day"] for d in plan["days"] for v in d["videos"]}
        for p in changed:                  # فقط تغییرهای واقعی؛ تیکِ دوباره‌ی یک ویدیوی تیک‌خورده رویداد نمی‌سازد
            log_event(cid, "video_done", ts=stamp, video=_tail(p), day=day_of[p],
                      done=req.done, source=req.source)
    return {"completed": plan["completed"], "updated_at": plan["updated_at"]}


@app.get("/api/programs/{cid}/rhythm")
def study_rhythm(cid: str):
    """ریتم واقعی مطالعه (از done_at ها) + تاریخچه‌ی تغییر برنامه‌ی روزانه (از لاگ رویدادها)."""
    meta, report, plan = load_program(cid)
    if not plan:
        raise HTTPException(404, "برای این برنامه هنوز برنامه‌ی روزانه ساخته نشده است.")
    try:
        data = ct.compute_rhythm(plan)
    except (KeyError, TypeError, ValueError):
        raise HTTPException(500, "فایل برنامه‌ی روزانه ساختار درستی ندارد.")
    changes = [e for e in read_events(cid) if e.get("type") == "plan_changed"]
    data["history_total"] = len(changes)
    data["history"] = list(reversed(changes))[:20]          # تازه‌ترین اول
    return data


@app.get("/api/programs/{cid}/download/{kind}")
def download_json(cid: str, kind: str):
    if kind not in ("report", "plan"):
        raise HTTPException(404, "نوع فایل نامعتبر است.")
    path = course_file(cid, kind)
    if not path.is_file():
        raise HTTPException(404, "فایل پیدا نشد.")
    return FileResponse(path, media_type="application/json",
                        filename=f"{ct.safe_filename(name_of(cid))}_{kind}.json")


# ----------------------------------------------------------------------
# انتخاب پوشه
# ----------------------------------------------------------------------
class PickRequest(BaseModel):
    initial: str = ""


@app.post("/api/pick-folder")
def pick_folder(req: PickRequest):
    """پنجره‌ی انتخاب پوشه‌ی خود سیستم را باز می‌کند. status: ok | cancel | unavailable"""
    status, path = st.pick_folder_native(clean_dir(req.initial))
    return {"status": status, "path": path}


@app.get("/api/fs")
def browse_dirs(path: str = Query("")):
    """فهرست زیرپوشه‌ها برای مرورگر پوشه‌ی داخل برنامه (وقتی پنجره‌ی سیستم در دسترس نیست)."""
    try:
        return st.list_dir(clean_dir(path))
    except PermissionError:
        raise HTTPException(403, "اجازه‌ی خواندن این پوشه را ندارید.")
    except OSError as exc:
        raise HTTPException(400, f"خطا در خواندن پوشه: {exc}")


# ----------------------------------------------------------------------
# تنظیمات پلیر
# ----------------------------------------------------------------------
DEFAULT_SETTINGS = {"configured": False, "resume": True,
                    "player": {"id": "default", "path": "", "args": ""}}


class SettingsRequest(BaseModel):
    player_id: str = "default"
    custom_path: str = ""
    custom_args: str = ""
    resume: bool = True


def load_settings() -> dict:
    data = read_optional(SETTINGS_FILE) or {}
    s = {k: data.get(k, v) for k, v in DEFAULT_SETTINGS.items()}
    s["player"] = {**DEFAULT_SETTINGS["player"], **(s["player"] or {})}
    return s


def settings_view() -> dict:
    s = load_settings()
    players = st.detect_players()
    cur = dict(s["player"])
    if cur["id"] == "default":
        name, tracking, ok = "پیش‌فرض سیستم", False, True
    elif cur["id"] == "custom":
        name, tracking, ok = "دلخواه", False, bool(cur.get("path")) and bool(
            shutil.which(cur["path"]) or os.path.isfile(cur["path"]))
    else:
        found = next((p for p in players if p["id"] == cur["id"]), None)
        name = found["name"] if found else cur["id"]
        tracking, ok = bool(found and found["tracking"]), found is not None
    cur.update(name=name, tracking=tracking, available=ok)
    return {"configured": s["configured"], "resume": s["resume"], "player": cur, "players": players}


def resolve_player(cfg: dict) -> dict:
    pid = cfg.get("id", "default")
    if pid == "default":
        return {"id": "default", "name": "پیش‌فرض سیستم", "path": ""}
    if pid == "custom":
        path = cfg.get("path", "")
        if not path or not (shutil.which(path) or os.path.isfile(path)):
            raise HTTPException(400, "پلیر دلخواه پیدا نشد. از دکمه‌ی «پلیر» مسیر درست را بدهید.")
        return {"id": "custom", "name": "دلخواه", "path": path, "args": cfg.get("args", "")}
    found = next((p for p in st.detect_players() if p["id"] == pid), None)
    if not found:
        raise HTTPException(400, "پلیر انتخاب‌شده روی این سیستم پیدا نشد. از دکمه‌ی «پلیر» یکی دیگر انتخاب کنید.")
    return found


@app.get("/api/settings")
def get_settings():
    return settings_view()


@app.put("/api/settings")
def put_settings(req: SettingsRequest):
    player = {"id": req.player_id, "path": "", "args": ""}
    if req.player_id == "custom":
        path = clean_dir(req.custom_path)
        exe = shutil.which(path) if path else None
        if not exe and path and os.path.isfile(path):
            exe = path
        if not exe:
            raise HTTPException(400, "فایل اجرایی پلیر پیدا نشد. مسیر کامل برنامه را بدهید.")
        player.update(path=exe, args=req.custom_args.strip())
    elif req.player_id != "default":
        found = next((p for p in st.detect_players() if p["id"] == req.player_id), None)
        if not found:
            raise HTTPException(400, "این پلیر روی سیستم پیدا نشد.")
        player["path"] = found["path"]
    with write_lock:
        ct.save_json({"configured": True, "resume": req.resume, "player": player}, SETTINGS_FILE)
    return settings_view()


# ----------------------------------------------------------------------
# پخش ویدیو و زمان توقف
# ----------------------------------------------------------------------
SESSIONS: dict = {}                 # پخش‌های در حال ردیابی: "<id>|<tail>" -> {cid, tail, proc}
sessions_lock = threading.Lock()


def _alive(entry: dict) -> bool:
    return entry["proc"] is None or entry["proc"].poll() is None


def active_tails(cid: str) -> List[str]:
    with sessions_lock:
        return [e["tail"] for e in SESSIONS.values() if e["cid"] == cid and _alive(e)]


def known_video_paths(report: Optional[dict], plan: Optional[dict]) -> set:
    paths = set()
    try:
        if report:
            paths |= {v["path"] for v in ct.flatten_videos(report["tree"])}
        if plan:
            paths |= {v["path"] for d in plan["days"] for v in d["videos"]}
    except (KeyError, TypeError):
        pass
    return paths


def save_position(cid: str, tail: str, seconds: float, duration: Optional[float] = None) -> None:
    """از رشته‌ی ردیاب پلیر صدا زده می‌شود. اگر برنامه در این فاصله حذف شده باشد، چیزی نمی‌نویسد."""
    try:
        with write_lock:
            if not (course_file(cid, "report").exists() or course_file(cid, "plan").exists()):
                return
            path = course_file(cid, "positions")
            data = read_optional(path) or {}
            old = data.get(tail) or {}
            data[tail] = {"seconds": round(seconds, 1),
                          "duration": round(duration, 1) if duration else old.get("duration"),
                          "updated_at": now(), "manual": False}
            ct.save_json(data, path)
    except (OSError, HTTPException):
        pass


def resume_point(pos: Optional[dict]) -> float:
    """از کجا ادامه بدهیم؟ نزدیک ابتدا یا انتها یعنی از اول."""
    if not pos:
        return 0.0
    sec, dur = pos.get("seconds") or 0, pos.get("duration")
    if sec < 5 or (dur and sec >= dur - 10):
        return 0.0
    return float(sec)


class PlayRequest(BaseModel):
    path: str


class PositionRequest(BaseModel):
    path: str
    seconds: Optional[float] = None       # None = پاک کردن


@app.post("/api/programs/{cid}/play")
def play_video(cid: str, req: PlayRequest):
    """ویدیو را با پلیر انتخاب‌شده‌ی کاربر باز می‌کند (فقط ویدیوهای همین برنامه)."""
    meta, report, plan = load_program(cid)
    if req.path not in known_video_paths(report, plan):
        raise HTTPException(400, "این ویدیو در این برنامه نیست.")
    root = meta.get("directory") or (report or {}).get("root_directory", "")
    if not root or not os.path.isdir(root):
        raise HTTPException(404, "پوشه‌ی دوره پیدا نشد. مسیر را در «ویرایش» اصلاح کنید.")
    tail = _tail(req.path)
    root_n = os.path.normpath(root)
    video = os.path.normpath(os.path.join(root_n, *tail.split("/")))
    if not video.startswith(root_n.rstrip(os.sep) + os.sep):
        raise HTTPException(400, "مسیر ویدیو نامعتبر است.")
    if Path(video).suffix.lower() not in ct.VIDEO_EXTENSIONS or not os.path.isfile(video):
        raise HTTPException(404, f"فایل ویدیو پیدا نشد: {tail}")

    settings = load_settings()
    player = resolve_player(settings["player"])
    key = f"{cid}|{tail}"
    with sessions_lock:
        running = SESSIONS.get(key)
        if running and _alive(running):
            return {"already_running": True, "player": player["name"], "tracking": True,
                    "can_start": True, "start": 0}

    positions = read_optional(course_file(cid, "positions")) or {}
    can_start = player["id"] in st.TRACKED
    start = resume_point(positions.get(tail)) if settings["resume"] and can_start else 0.0

    entry = {"cid": cid, "tail": tail, "proc": None}
    with sessions_lock:
        SESSIONS[key] = entry

    started_at = ct.now_stamp()

    def log_session(tracking: bool, watched: Optional[float] = None) -> None:
        log_event(cid, "play_session", video=tail, player=player["id"], tracking=tracking,
                  started_at=started_at, ended_at=ct.now_stamp() if tracking else None,
                  watched_seconds=watched)

    def on_exit(watched: Optional[float] = None) -> None:
        with sessions_lock:
            if SESSIONS.get(key) is entry:
                SESSIONS.pop(key, None)
        if watched is not None:            # ردیاب پلیر (mpv/VLC) بعد از بسته‌شدن پلیر با عدد صدا می‌زند
            log_session(True, watched)

    try:
        info = st.start_player(player, video, start, player.get("args", ""),
                               on_update=lambda sec, dur: save_position(cid, tail, sec, dur),
                               on_exit=on_exit)
    except (OSError, ValueError) as exc:
        on_exit()
        raise HTTPException(500, f"اجرای پلیر ناموفق بود: {exc}")
    entry["proc"] = info["proc"]
    if not info["tracking"]:
        on_exit()
        log_session(False)                 # پایان این پلیرها معلوم نیست؛ فقط شروع ثبت می‌شود
    return {"already_running": False, "player": player["name"], "tracking": info["tracking"],
            "can_start": info["can_start"], "start": start}


@app.get("/api/programs/{cid}/positions")
def get_positions(cid: str):
    return {"positions": read_optional(course_file(cid, "positions")) or {},
            "active": active_tails(cid)}


@app.put("/api/programs/{cid}/position")
def set_position(cid: str, req: PositionRequest):
    """ثبت یا پاک کردن دستیِ زمان توقف (برای پلیرهایی که خودکار ثبت نمی‌کنند)."""
    meta, report, plan = load_program(cid)
    if req.path not in known_video_paths(report, plan):
        raise HTTPException(400, "این ویدیو در این برنامه نیست.")
    tail = _tail(req.path)
    if req.seconds is not None and not (0 <= req.seconds <= 24 * 3600):
        raise HTTPException(400, "زمان توقف باید بین ۰ تا ۲۴ ساعت باشد.")
    path = course_file(cid, "positions")
    with write_lock:
        data = read_optional(path) or {}
        if req.seconds is None:
            data.pop(tail, None)
        else:
            old = data.get(tail) or {}
            data[tail] = {"seconds": round(req.seconds, 1), "duration": old.get("duration"),
                          "updated_at": now(), "manual": True}
        ct.save_json(data, path)
    return {"positions": data}


if __name__ == "__main__":
    import uvicorn

    # فقط روی 127.0.0.1 گوش می‌دهد، یعنی فقط از همین کامپیوتر قابل دسترسی است
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "18000")))