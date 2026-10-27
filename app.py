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
<id>_events.jsonl لاگ فقط-افزودنی رویدادها (هر خط یک JSON): plan_changed، video_done، play_session.
profile.json نام و شناسه‌ی کاربر (user_id) و فهرست دستگاه‌ها. مسیر هر دوره روی هر دستگاه در <id>_meta.json
(locations و active) است. اطلاعات نسخه‌ی اپ اندروید از android_release.json کنار app.py خوانده می‌شود. تنظیمات پلیر در data/settings.json است.
کارهای وابسته به سیستم‌عامل (انتخاب پوشه، پلیر) در system_tools.py هستند.

اجرا:
    python app.py
سپس در مرورگر:  http://127.0.0.1:18000
"""
import json
import os
import re
import secrets
import shutil
import socket
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional
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
    directory: str            # مسیر پوشه روی همین دستگاه (اسکن می‌شود)
    device_paths: Dict[str, str] = {}    # اختیاری: {شناسه‌ی دستگاهِ دیگر: مسیر}؛ فقط ذخیره می‌شود


class ProgramPatch(BaseModel):
    name: Optional[str] = None
    directory: Optional[str] = None      # مسیر همین دستگاه (کوتاه‌نویسی برای device_paths[این دستگاه])
    device_paths: Dict[str, str] = {}    # {شناسه‌ی دستگاه: مسیر فعال}؛ فقط دستگاه‌های تغییرکرده
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


# ----------------------------------------------------------------------
# پروفایل، دستگاه‌ها و مسیر دوره روی هر دستگاه
# ----------------------------------------------------------------------
PROFILE_FILE = DATA_DIR / "profile.json"
ANDROID_RELEASE_FILE = Path(os.environ.get("ROOTEND_ANDROID_RELEASE") or BASE_DIR / "android_release.json").expanduser()
PLATFORMS = ("windows", "linux", "mac", "android", "ios")
MAX_LOCATIONS, MAX_DEVICES, MAX_PATH = 50, 20, 1000
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
USER_ID_RE = re.compile(r"^u_[A-Za-z0-9_-]{20,64}$")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def this_platform() -> str:
    if os.name == "nt":
        return "windows"
    return "mac" if sys.platform == "darwin" else "linux"


def load_profile() -> dict:
    """
    profile.json: نام و شناسه‌ی کاربر (اگر وارد شده) + فهرست دستگاه‌ها.
    بار اول فقط «همین دستگاه» ساخته می‌شود. فایل خراب را با فایل تازه جایگزین نمی‌کنیم
    (شناسه‌ی کاربر از دست نرود)؛ خطا می‌دهیم.
    """
    with write_lock:
        data = read_json(PROFILE_FILE) if PROFILE_FILE.exists() else {}
        changed = not data
        data.setdefault("user_id", None)
        data.setdefault("name", "")
        devices = data.setdefault("devices", [])
        if not any(d.get("id") == data.get("current_device") for d in devices):
            did = "d_" + secrets.token_hex(4)
            devices.append({"id": did, "name": device_host(), "platform": this_platform(), "host": device_host()})
            data["current_device"] = did
            changed = True
        if changed:
            ct.save_json(data, PROFILE_FILE)
        return data


def profile_view(p: dict) -> dict:
    return {"signed_in": bool(p.get("user_id")), "user_id": p.get("user_id"), "name": p.get("name", ""),
            "signed_in_at": p.get("signed_in_at"), "current_device": p["current_device"], "devices": p["devices"]}


def clean_label(text: Optional[str], limit: int, what: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise HTTPException(400, f"{what} نمی‌تواند خالی باشد.")
    if len(text) > limit:
        raise HTTPException(400, f"{what} حداکثر {limit} نویسه می‌تواند باشد.")
    return text


def clean_version(value: Optional[str]) -> str:
    v = (value or "").strip()
    if v and not VERSION_RE.match(v):
        raise HTTPException(400, "نسخه را مثل 1.4.0 وارد کنید (فقط عدد و نقطه).")
    return v


def version_tuple(v) -> Optional[tuple]:
    if not isinstance(v, str) or not VERSION_RE.match(v.strip()):
        return None
    parts = [int(x) for x in v.strip().split(".")]
    return tuple(parts + [0] * (4 - len(parts)))


def new_location_id() -> str:
    return "l_" + secrets.token_hex(4)


def locations_of(meta: dict, report: Optional[dict], profile: dict):
    """
    (locations, active) یک برنامه. هر مسیر: {"id", "device", "path"}؛ active: {شناسه‌ی دستگاه: شناسه‌ی مسیر}.
    برنامه‌ی قدیمی که فقط یک directory داشت، یک مسیر برای همین دستگاه حساب می‌شود.
    """
    locs = meta.get("locations")
    if isinstance(locs, list):
        good = [l for l in locs if isinstance(l, dict) and l.get("id") and l.get("device") and l.get("path")]
        active = meta.get("active")
        return good, active if isinstance(active, dict) else {}
    old = meta.get("directory") or (report or {}).get("root_directory", "")
    if not old:
        return [], {}
    dev = profile["current_device"]
    return [{"id": "l_main", "device": dev, "path": old}], {dev: "l_main"}


def normalize_active(locs: List[dict], active: Optional[dict]) -> dict:
    """برای هر دستگاهی که مسیر دارد، یک مسیر فعال: انتخاب‌شده اگر معتبر باشد، وگرنه اولی."""
    out = {}
    for dev in dict.fromkeys(l["device"] for l in locs):
        mine = [l for l in locs if l["device"] == dev]
        out[dev] = next((l for l in mine if l["id"] == (active or {}).get(dev)), mine[0])["id"]
    return out


def path_for(locs: List[dict], active: Optional[dict], device: str) -> str:
    lid = normalize_active(locs, active).get(device)
    return next((l["path"] for l in locs if l["id"] == lid), "")


def effective_directory(meta: dict, report: Optional[dict]) -> str:
    """مسیر فعالِ این برنامه روی همین دستگاه (خالی = برای این دستگاه مسیری تعریف نشده)."""
    profile = load_profile()
    locs, active = locations_of(meta, report, profile)
    return path_for(locs, active, profile["current_device"])


def check_device_paths(paths: Optional[Dict[str, str]]) -> Dict[str, str]:
    """{دستگاه: مسیر} ورودی کاربر را بررسی و تمیز می‌کند."""
    known = {d["id"] for d in load_profile()["devices"]}
    out: Dict[str, str] = {}
    for dev, raw in (paths or {}).items():
        path = clean_dir(raw)
        if dev not in known:
            raise HTTPException(400, "دستگاه ناشناخته است. اول آن را در صفحه‌ی «حساب» اضافه کنید.")
        if not path or len(path) > MAX_PATH:
            raise HTTPException(400, "مسیر نمی‌تواند خالی یا بیش از حد بلند باشد.")
        out[dev] = path
    return out


def apply_device_path(locs: List[dict], active: dict, device: str, path: str, is_here: bool):
    """
    مسیر فعالِ یک دستگاه را روی path می‌گذارد:
      - همین مسیر قبلاً برای آن دستگاه ثبت بوده -> همان فعال می‌شود
      - دستگاه یک مسیر فعال دارد -> همان مسیر در جا ویرایش می‌شود (غلط املایی/جابه‌جایی پوشه)
      - دستگاه هنوز مسیری ندارد -> مسیر تازه ساخته و فعال می‌شود
    """
    def same(a: str, b: str) -> bool:
        return norm_path(a) == norm_path(b) if is_here else a.rstrip("/\\") == b.rstrip("/\\")
    mine = [l for l in locs if l["device"] == device]
    hit = next((l for l in mine if same(l["path"], path)), None)
    if hit:
        return locs, {**active, device: hit["id"]}
    act = next((l for l in mine if l["id"] == active.get(device)), None)
    if act:
        return [{**l, "path": path} if l["id"] == act["id"] else l for l in locs], active
    new = {"id": new_location_id(), "device": device, "path": path}
    return locs + [new], {**active, device: new["id"]}


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
    profile = load_profile()
    here = profile["current_device"]
    locs, active = locations_of(meta, report, profile)
    active = normalize_active(locs, active)
    directory = next((l["path"] for l in locs if l["id"] == active.get(here)), "")

    def is_dir(path: str) -> bool:
        try:
            return bool(path) and Path(path).is_dir()
        except OSError:
            return False
    exists = is_dir(directory)
    # برای دستگاه‌های دیگر نمی‌شود وجود پوشه را بررسی کرد (exists = None)
    loc_view = [{**l, "exists": is_dir(l["path"]) if l["device"] == here else None} for l in locs]
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
        "locations": loc_view,
        "active": active,
        "current_device": here,
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
    others = check_device_paths(req.device_paths)
    if load_profile()["current_device"] in others:
        raise HTTPException(400, "مسیر همین دستگاه را در فیلد مسیر اصلی بدهید.")
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
        here, lid = load_profile()["current_device"], new_location_id()
        locs = [{"id": lid, "device": here, "path": report["root_directory"]}]
        active = {here: lid}
        for dev, path in others.items():
            locs, active = apply_device_path(locs, active, dev, path, False)
        ct.save_json({"name": name, "created_at": stamp, "updated_at": stamp,
                      "locations": locs, "active": active}, course_file(cid, "meta"))
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
        "devices": load_profile()["devices"],
    }


@app.patch("/api/programs/{cid}")
def update_program(cid: str, req: ProgramPatch):
    """
    تغییر نام و/یا مسیر پوشه روی هر دستگاه (device_paths)، یا اسکن مجدد.
    اگر مسیرِ همین دستگاه عوض شود پوشه‌ی جدید دوباره اسکن می‌شود؛ مسیر دستگاه‌های دیگر فقط ذخیره می‌شود.
    """
    meta, report, plan = load_program(cid)
    current = summarize(cid, meta, report, plan)
    here = current["current_device"]

    name = None
    if req.name is not None:
        name = clean_name(req.name)
        if not name:
            raise HTTPException(400, "نام برنامه نمی‌تواند خالی باشد.")
        if name != current["name"]:
            ensure_name_free(name, exclude=cid)

    wanted = check_device_paths(req.device_paths)
    if req.directory is not None:
        directory = clean_dir(req.directory)
        if not directory:
            raise HTTPException(400, "مسیر پوشه را وارد کنید.")
        wanted[here] = directory
    dir_changed = here in wanted and (
        not current["directory"] or norm_path(wanted[here]) != norm_path(current["directory"]))

    new_report = None
    if dir_changed or req.rescan:
        new_report = scan_directory(wanted[here] if dir_changed else current["directory"])

    with write_lock:
        meta = read_optional(course_file(cid, "meta")) or {}
        meta.setdefault("created_at", current["created_at"] or now())
        meta["name"] = name if name is not None else meta.get("name") or current["name"]
        profile = load_profile()
        locs, active = locations_of(meta, report, profile)
        active = normalize_active(locs, active)
        for dev, path in wanted.items():
            if dev == here and dir_changed:
                path = new_report["root_directory"]       # همان‌طور که اسکن شد (مطلق و کامل)
            locs, active = apply_device_path(locs, active, dev, path, dev == here)
        if new_report is not None:
            ct.save_json(new_report, course_file(cid, "report"))
            report = new_report
        meta["locations"], meta["active"] = locs, normalize_active(locs, active)
        meta.pop("directory", None)
        meta["updated_at"] = now()
        ct.save_json(meta, course_file(cid, "meta"))

    summary = summarize(cid, meta, report, plan)
    return {
        "program": summary,
        "rescanned": new_report is not None,
        "video_count": summary["video_count"],
        "failed": len(new_report["failed_files"]) if new_report else 0,
    }


class LocationItem(BaseModel):
    id: str = ""                   # خالی = مسیر جدید
    device: str
    path: str


class LocationsRequest(BaseModel):
    locations: List[LocationItem]
    active: Dict[str, str] = {}    # {شناسه‌ی دستگاه: شناسه‌ی مسیر فعال}


@app.put("/api/programs/{cid}/locations")
def put_locations(cid: str, req: LocationsRequest):
    """
    مسیر این دوره روی هر دستگاه (کل فهرست را جایگزین می‌کند).
    اگر مسیر فعالِ همین دستگاه عوض شود، پوشه‌ی جدید دوباره اسکن می‌شود؛ برای دستگاه‌های دیگر فقط ذخیره می‌شود.
    """
    meta, report, plan = load_program(cid)
    profile = load_profile()
    here = profile["current_device"]
    known = {d["id"] for d in profile["devices"]}
    if len(req.locations) > MAX_LOCATIONS:
        raise HTTPException(400, f"حداکثر {MAX_LOCATIONS} مسیر برای یک برنامه.")

    locs, ids, seen = [], set(), set()
    for item in req.locations:
        path = clean_dir(item.path)
        if not path or len(path) > MAX_PATH:
            raise HTTPException(400, "مسیر نمی‌تواند خالی یا بیش از حد بلند باشد.")
        if item.device not in known:
            raise HTTPException(400, "دستگاه ناشناخته است. اول آن را در صفحه‌ی «حساب» اضافه کنید.")
        lid = item.id or new_location_id()
        if not ID_RE.match(lid) or lid in ids:
            raise HTTPException(400, "شناسه‌ی مسیر نامعتبر است.")
        key = (item.device, norm_path(path) if item.device == here else path.rstrip("/\\"))
        if key in seen:
            raise HTTPException(400, f"این مسیر برای این دستگاه دو بار آمده است: {path}")
        ids.add(lid)
        seen.add(key)
        locs.append({"id": lid, "device": item.device, "path": path})
    active = normalize_active(locs, req.active)

    old_here = effective_directory(meta, report)
    new_here = path_for(locs, active, here)
    new_report = None
    if new_here and (not old_here or norm_path(new_here) != norm_path(old_here)):
        new_report = scan_directory(new_here)        # خطا بدهد، چیزی ذخیره نمی‌شود

    with write_lock:
        meta = read_optional(course_file(cid, "meta")) or {}
        meta.setdefault("created_at", now())
        meta.setdefault("name", name_of(cid))
        meta["locations"], meta["active"] = locs, active
        meta.pop("directory", None)
        meta["updated_at"] = now()
        if new_report is not None:
            ct.save_json(new_report, course_file(cid, "report"))
            report = new_report
        ct.save_json(meta, course_file(cid, "meta"))

    summary = summarize(cid, meta, report, plan)
    return {"program": summary, "rescanned": new_report is not None, "video_count": summary["video_count"],
            "failed": len(new_report["failed_files"]) if new_report else 0}


# ----------------------------------------------------------------------
# API: پروفایل (ورود محلی)، دستگاه‌ها، اطلاعات نسخه‌ی اپ اندروید
# ----------------------------------------------------------------------
class LoginRequest(BaseModel):
    name: str
    user_id: str = ""           # خالی = شناسه‌ی تازه ساخته می‌شود


class ProfilePatch(BaseModel):
    name: str


class DeviceRequest(BaseModel):
    name: Optional[str] = None
    platform: Optional[str] = None
    app_version: Optional[str] = None


def check_platform(value: Optional[str]) -> str:
    if value not in PLATFORMS:
        raise HTTPException(400, "نوع دستگاه نامعتبر است.")
    return value


def device_name_free(prof: dict, name: str, exclude: Optional[str] = None) -> None:
    if any(d["name"].casefold() == name.casefold() for d in prof["devices"] if d["id"] != exclude):
        raise HTTPException(409, f"دستگاهی با نام «{name}» از قبل هست. نام دیگری بدهید.")


@app.get("/api/profile")
def get_profile():
    return profile_view(load_profile())


@app.post("/api/profile/login")
def login(req: LoginRequest):
    """بدون سرور و بدون رمز: فقط نام و شناسه روی همین کامپیوتر ذخیره می‌شود. شناسه‌ی خالی = ساخت شناسه‌ی جدید."""
    name = clean_label(req.name, 60, "نام")
    uid = (req.user_id or "").strip()
    if uid and not USER_ID_RE.match(uid):
        raise HTTPException(400, "شناسه درست نیست. شناسه با u_ شروع می‌شود و حداقل ۲۰ نویسه‌ی بعد از آن دارد.")
    with write_lock:
        prof = load_profile()
        if prof.get("user_id"):
            raise HTTPException(409, "الان وارد شده‌اید. اول خارج شوید.")
        prof["user_id"] = uid or "u_" + secrets.token_urlsafe(16)
        prof["name"] = name
        prof["signed_in_at"] = ct.now_stamp()
        ct.save_json(prof, PROFILE_FILE)
    return profile_view(prof)


@app.post("/api/profile/logout")
def logout():
    """شناسه و نام را از این دستگاه برمی‌دارد؛ برنامه‌ها و دستگاه‌ها دست‌نخورده می‌مانند."""
    with write_lock:
        prof = load_profile()
        prof["user_id"], prof["name"] = None, ""
        prof.pop("signed_in_at", None)
        ct.save_json(prof, PROFILE_FILE)
    return profile_view(prof)


@app.patch("/api/profile")
def rename_profile(req: ProfilePatch):
    name = clean_label(req.name, 60, "نام")
    with write_lock:
        prof = load_profile()
        if not prof.get("user_id"):
            raise HTTPException(409, "اول وارد شوید.")
        prof["name"] = name
        ct.save_json(prof, PROFILE_FILE)
    return profile_view(prof)


@app.post("/api/profile/devices")
def add_device(req: DeviceRequest):
    name = clean_label(req.name, 40, "نام دستگاه")
    platform = check_platform(req.platform)
    with write_lock:
        prof = load_profile()
        if len(prof["devices"]) >= MAX_DEVICES:
            raise HTTPException(400, f"حداکثر {MAX_DEVICES} دستگاه.")
        device_name_free(prof, name)
        dev = {"id": "d_" + secrets.token_hex(4), "name": name, "platform": platform}
        if platform == "android" and clean_version(req.app_version):
            dev["app_version"] = clean_version(req.app_version)
        prof["devices"].append(dev)
        ct.save_json(prof, PROFILE_FILE)
    return profile_view(prof)


@app.patch("/api/profile/devices/{did}")
def edit_device(did: str, req: DeviceRequest):
    with write_lock:
        prof = load_profile()
        dev = next((d for d in prof["devices"] if d["id"] == did), None)
        if dev is None:
            raise HTTPException(404, "دستگاه پیدا نشد.")
        if req.name is not None:
            name = clean_label(req.name, 40, "نام دستگاه")
            device_name_free(prof, name, exclude=did)
            dev["name"] = name
        if req.platform is not None:
            dev["platform"] = check_platform(req.platform)
        if dev["platform"] != "android":
            dev.pop("app_version", None)
        elif req.app_version is not None:
            version = clean_version(req.app_version)
            if version:
                dev["app_version"] = version
            else:
                dev.pop("app_version", None)
        ct.save_json(prof, PROFILE_FILE)
    return profile_view(prof)


@app.delete("/api/profile/devices/{did}")
def delete_device(did: str):
    """دستگاه را با مسیرهایش در همه‌ی برنامه‌ها حذف می‌کند (همین دستگاه قابل حذف نیست)."""
    with write_lock:
        prof = load_profile()
        if did == prof["current_device"]:
            raise HTTPException(400, "دستگاهی که الان با آن کار می‌کنید حذف نمی‌شود.")
        if not any(d["id"] == did for d in prof["devices"]):
            raise HTTPException(404, "دستگاه پیدا نشد.")
        prof["devices"] = [d for d in prof["devices"] if d["id"] != did]
        ct.save_json(prof, PROFILE_FILE)
        for cid in existing_ids():
            meta = read_optional(course_file(cid, "meta"))
            if not meta or not isinstance(meta.get("locations"), list):
                continue
            keep = [l for l in meta["locations"] if l.get("device") != did]
            if len(keep) != len(meta["locations"]):
                meta["locations"], meta["active"] = keep, normalize_active(keep, meta.get("active"))
                meta["updated_at"] = now()
                ct.save_json(meta, course_file(cid, "meta"))
    return profile_view(prof)


def safe_url(value) -> str:
    """فقط http(s) یا مسیر داخل همین سایت (/...)؛ مثلاً javascript: رد می‌شود."""
    u = value.strip() if isinstance(value, str) else ""
    ok = re.match(r"^https?://[^\s<>\"']+$", u) or re.match(r"^/(?!/)[^\s<>\"']*$", u)
    return u if ok else ""


@app.get("/api/android-update")
def android_update():
    """آخرین نسخه‌ی اپ اندروید (از android_release.json) و اینکه هر دستگاه اندرویدی آپدیت لازم دارد یا نه."""
    prof = load_profile()
    out = {"configured": False, "error": "", "release": None, "devices": []}
    latest = None
    if ANDROID_RELEASE_FILE.is_file():
        try:
            with open(ANDROID_RELEASE_FILE, encoding="utf-8") as f:
                raw = json.load(f)
            latest = version_tuple(raw.get("version"))
            if latest is None:
                out["error"] = "در android_release.json مقدار version باید مثل 1.4.0 باشد."
            else:
                log = raw.get("changelog") or []
                log = [log] if isinstance(log, str) else log if isinstance(log, list) else []
                sha = str(raw.get("sha256") or "").strip().lower()
                out["release"] = {
                    "version": raw["version"].strip(),
                    "released_at": str(raw.get("released_at") or "")[:32],
                    "changelog": [str(x)[:300] for x in log[:20]],
                    "direct_url": safe_url(raw.get("apk_url")),
                    "sha256": sha if re.fullmatch(r"[0-9a-f]{64}", sha) else "",
                    "play_url": safe_url(raw.get("play_url")),
                }
                out["configured"] = True
        except (OSError, ValueError, AttributeError):
            out["error"] = "فایل android_release.json خوانده نشد یا JSON درستی نیست."
    for d in prof["devices"]:
        if d.get("platform") != "android":
            continue
        have = version_tuple(d.get("app_version"))
        out["devices"].append({"id": d["id"], "name": d["name"], "app_version": d.get("app_version", ""),
                               "update_available": (have < latest) if (have and latest) else None})
    return out


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
    tail = _tail(req.path)
    found = locate_here(cid, tail)           # مسیر فعال ← بقیه‌ی مسیرهای این دستگاه ← شناسایی دوباره‌ی دستگاه
    if not found["ok"]:
        raise HTTPException(404, {"code": found["code"], "message": found["message"]})
    video = found["video"]

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
        "can_start": info["can_start"], "start": start,
        "relocated": found["relocated"], "note": found["note"]}


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


# ----------------------------------------------------------------------
# شناسایی «این دستگاه» و پیدا کردن مسیر دوره روی همین دستگاه
# ----------------------------------------------------------------------
def device_host() -> str:
    return socket.gethostname() or "this device"


def detect_device() -> dict:
    """
    «این دستگاه» را دوباره شناسایی می‌کند (با نام کامپیوتر + نوع سیستم‌عامل).
    کاربرد: پروفایل یا برنامه‌ها از دستگاه دیگری کپی/همگام شده‌اند و current_device هنوز به دستگاه قبلی اشاره می‌کند.
      ok        همین دستگاه درست بود (در پروفایل قدیمی بدون host، فقط نام کامپیوتر ثبت می‌شود)
      switched  به دستگاهِ هم‌نامی که قبلاً ثبت شده بود منتقل شد
      created   دستگاه جدیدی ساخته شد (مسیر دوره‌ها باید برایش تعریف شود)
      full      جای دستگاه جدید نیست
    """
    host, plat = device_host(), this_platform()
    with write_lock:
        prof = load_profile()
        devs = prof["devices"]
        cur = next(d for d in devs if d["id"] == prof["current_device"])
        status, changed = "ok", False
        if cur.get("host") is None and cur.get("platform") == plat:
            cur["host"], changed = host, True
        elif cur.get("platform") != plat or cur.get("host", host) != host:
            match = next((d for d in devs if d.get("host") == host and d.get("platform") == plat), None)
            if match:
                prof["current_device"], status, changed = match["id"], "switched", True
            elif len(devs) >= MAX_DEVICES:
                status = "full"
            else:
                taken = {d["name"].casefold() for d in devs}
                name, n = host, 2
                while name.casefold() in taken:
                    name, n = f"{host} ({n})", n + 1
                new_dev = {"id": "d_" + secrets.token_hex(4), "name": name, "platform": plat, "host": host}
                devs.append(new_dev)
                prof["current_device"], status, changed = new_dev["id"], "created", True
        if changed:
            ct.save_json(prof, PROFILE_FILE)
        dev = next(d for d in devs if d["id"] == prof["current_device"])
    text = {
        "ok": f"دستگاه درست شناسایی شد: «{dev['name']}».",
        "switched": f"دستگاه دوباره شناسایی شد: «{dev['name']}».",
        "created": f"این دستگاه قبلاً ثبت نشده بود؛ «{dev['name']}» ساخته شد. مسیر هر دوره را باید روی آن تعریف کنید.",
        "full": "این دستگاه با دستگاه ثبت‌شده نمی‌خواند ولی جایی برای دستگاه جدید نیست. در صفحه‌ی «حساب» یکی را حذف کنید.",
    }[status]
    return {"status": status, "id": dev["id"], "name": dev["name"], "message": text, "profile": profile_view(prof)}


def _video_in(root: str, tail: str) -> Optional[str]:
    """مسیر کامل ویدیو داخل root؛ اگر پوشه یا فایل نبود None."""
    if not root or not os.path.isdir(root):
        return None
    root_n = os.path.normpath(root)
    video = os.path.normpath(os.path.join(root_n, *tail.split("/")))
    if not video.startswith(root_n.rstrip(os.sep) + os.sep):
        raise HTTPException(400, "مسیر ویدیو نامعتبر است.")
    if Path(video).suffix.lower() not in ct.VIDEO_EXTENSIONS or not os.path.isfile(video):
        return None
    return video


def _probe(cid: str, tail: Optional[str]) -> dict:
    """مسیرهای ثبت‌شده‌ی این دوره برای همین دستگاه را (اول مسیر فعال) می‌آزماید. اگر tail داده شود، ویدیو هم باید باشد."""
    meta, report, _plan = load_program(cid)
    prof = load_profile()
    here = prof["current_device"]
    locs, active = locations_of(meta, report, prof)
    act = normalize_active(locs, active).get(here)
    mine = sorted((l for l in locs if l["device"] == here), key=lambda l: l["id"] != act)
    chosen = video = None
    for l in mine:
        video = _video_in(l["path"], tail) if tail else None
        if video or (not tail and os.path.isdir(l["path"])):
            chosen = l
            break
    name = next((d["name"] for d in prof["devices"] if d["id"] == here), "این دستگاه")
    return {"here": here, "name": name, "mine": mine, "act": act, "chosen": chosen, "video": video}


def set_active_location(cid: str, device: str, lid: str) -> None:
    """مسیر فعالِ یک دستگاه را (بدون اسکن مجدد) عوض می‌کند."""
    with write_lock:
        meta = read_optional(course_file(cid, "meta")) or {}
        report = read_optional(course_file(cid, "report"))
        locs, active = locations_of(meta, report, load_profile())
        active = normalize_active(locs, active)
        if not any(l["id"] == lid and l["device"] == device for l in locs):
            return
        active[device] = lid
        meta.setdefault("created_at", now())
        meta.setdefault("name", name_of(cid))
        meta["locations"], meta["active"] = locs, active
        meta.pop("directory", None)
        meta["updated_at"] = now()
        ct.save_json(meta, course_file(cid, "meta"))


def locate_here(cid: str, tail: Optional[str] = None, redetect: bool = True) -> dict:
    """
    مسیر دوره (و با دادن tail، فایل ویدیو) را روی همین دستگاه پیدا می‌کند:
      ۱) مسیر فعال  ۲) بقیه‌ی مسیرهای ثبت‌شده‌ی همین دستگاه (اولینِ سالم فعال می‌شود)
      ۳) اگر نشد و redetect=True: دستگاه دوباره شناسایی می‌شود و یک بار دیگر امتحان می‌کنیم
    """
    pr = _probe(cid, tail)
    device = None
    if not pr["chosen"] and redetect:
        device = detect_device()
        if device["status"] in ("switched", "created"):
            pr = _probe(cid, tail)
    if pr["chosen"]:
        switched = pr["chosen"]["id"] != pr["act"]
        if switched:
            set_active_location(cid, pr["here"], pr["chosen"]["id"])
        notes = []
        if device and device["status"] in ("switched", "created"):
            notes.append(device["message"])
        if switched:
            notes.append(f"مسیر فعال این دوره روی این دستگاه خودکار به «{pr['chosen']['path']}» تغییر کرد.")
        return {"ok": True, "video": pr["video"], "path": pr["chosen"]["path"], "switched": switched,
                "relocated": bool(notes), "note": " ".join(notes), "device": device}
    name = pr["name"]
    if not pr["mine"]:
        code = "no_path"
        msg = (f"این دوره روی «{name}» مسیری ندارد. برای پخش، مسیر پوشه‌ی دوره را روی همین دستگاه تعریف کنید: "
               "تب «مسیرها» ← «انتخاب پوشه…» یا نوشتن مسیر ← «افزودن مسیر».")
    elif not any(os.path.isdir(l["path"]) for l in pr["mine"]):
        code = "dir_missing"
        msg = (f"پوشه‌ی دوره روی «{name}» پیدا نشد ({pr['mine'][0]['path']}). دستگاه و مسیرهای ثبت‌شده دوباره بررسی شد "
               "و مسیر سالمی نبود. مسیر درست را در تب «مسیرها» تعریف کنید.")
    else:
        code = "file_missing"
        msg = (f"پوشه‌ی دوره هست ولی فایل ویدیو در آن پیدا نشد: {tail}. اگر پوشه تغییر کرده، «اسکن مجدد» بزنید "
               "یا مسیر درست را در تب «مسیرها» تعریف کنید.")
    return {"ok": False, "code": code, "message": msg, "device": device}


@app.post("/api/profile/detect-device")
def detect_device_api():
    """دکمه‌ی «شناسایی مجدد دستگاه» (وقتی برنامه‌ی خاصی باز نیست)."""
    return detect_device()


@app.post("/api/programs/{cid}/relocate")
def relocate_program(cid: str):
    """دکمه‌ی «شناسایی مجدد دستگاه و مسیر»: دستگاه را دوباره شناسایی می‌کند و برای این دوره، مسیری را که واقعاً هست فعال می‌کند."""
    load_program(cid)
    det = detect_device()
    res = locate_here(cid, redetect=False)
    parts = [det["message"]]
    if res["ok"]:
        parts.append(f"مسیر فعال روی این دستگاه: {res['path']}"
                     + (" (از بین مسیرهای ثبت‌شده انتخاب شد)" if res["switched"] else ""))
    else:
        parts.append(res["message"])
    meta, report, plan = load_program(cid)
    return {"found": res["ok"], "code": None if res["ok"] else res["code"], "message": " ".join(parts),
            "device": {k: det[k] for k in ("status", "id", "name")}, "profile": det["profile"],
            "program": summarize(cid, meta, report, plan)}

if __name__ == "__main__":
    import uvicorn

    # فقط روی 127.0.0.1 گوش می‌دهد، یعنی فقط از همین کامپیوتر قابل دسترسی است
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "18000")))