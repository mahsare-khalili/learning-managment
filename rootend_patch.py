#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rootend_patch.py - تغییر نام پروژه به rootend + افزودن لوگو

rootend = root + tend  ->  «مراقبت از ریشه‌ها»

این اسکریپت فقط خط‌های مشخصی از فایل‌های پروژه را تغییر می‌دهد (نه کل فایل‌ها را):
  app.py, system_tools.py, course_timer.py, static/index.html

  * نام برنامه (عنوان صفحه، FastAPI، توضیح‌ها و کامنت‌ها) -> rootend
  * لوگو در هدر، فاوآیکن و صفحه‌ی خالی؛ سرو کردن پوشه‌ی assets/ از آدرس /assets
  * متغیرهای محیطی ROOTEND_DATA_DIR و ROOTEND_ALLOWED_HOSTS (نام‌های قدیمی COURSE_* هم هنوز کار می‌کنند)

ایمن است:
  * اگر هر خطِ لازم در یک فایل پیدا نشود، آن فایل اصلاً نوشته نمی‌شود.
  * اجرای دوباره آسیبی نمی‌زند (تغییرهای انجام‌شده را رد می‌کند).

استفاده:
    python3 rootend_patch.py /opt/ai/projects/course_web
"""
import re
import sys
from pathlib import Path

LOGO_SVG = "/assets/images/rootend_logo_accurate.svg"
LOGO_JPG = "/assets/images/rootend_logo_accurate.jpg"

# هر تغییر: (برچسب، الگوی regex، جایگزین، نشانه‌ی «قبلاً انجام شده»، آیا نبودنش خطاست؟، تعداد (0 = همه))
APP_PY = [
    ("docstring", r"^app\.py - [^\n]*$",
     "app.py - rootend (root + tend: مراقبت از ریشه‌ها)، برنامه‌ی وب FastAPI برای برنامه‌ریزی و پیگیری دوره‌های آموزشی",
     "app.py - rootend", True, 1),
    ("import StaticFiles", r"^from fastapi\.responses import FileResponse, JSONResponse$",
     "from fastapi.responses import FileResponse, JSONResponse\nfrom fastapi.staticfiles import StaticFiles",
     "from fastapi.staticfiles import StaticFiles", True, 1),
    ("data dir env", r'os\.environ\.get\("COURSE_DATA_DIR", BASE_DIR / "data"\)',
     'os.environ.get("ROOTEND_DATA_DIR") or os.environ.get("COURSE_DATA_DIR") or BASE_DIR / "data"',
     'os.environ.get("ROOTEND_DATA_DIR")', True, 1),
    ("data dir comment", r"(?m)^(#[^\n]*)COURSE_DATA_DIR", r"\g<1>ROOTEND_DATA_DIR", None, False, 1),
    ("allowed hosts env", r'os\.environ\.get\("COURSE_ALLOWED_HOSTS", ""\)',
     '(os.environ.get("ROOTEND_ALLOWED_HOSTS") or os.environ.get("COURSE_ALLOWED_HOSTS") or "")',
     'os.environ.get("ROOTEND_ALLOWED_HOSTS")', True, 1),
    ("allowed hosts comment", r"(?m)^(#[^\n]*)COURSE_ALLOWED_HOSTS", r"\g<1>ROOTEND_ALLOWED_HOSTS", None, False, 1),
    ("FastAPI title + assets", r'app = FastAPI\(title="Course Planner"\)',
     'app = FastAPI(title="rootend")\n\n'
     '# لوگو و تصویرهای ثابت: assets/images/... از آدرس /assets/images/... در دسترس است\n'
     'ASSETS_DIR = BASE_DIR / "assets"\n'
     'if ASSETS_DIR.is_dir():\n'
     '    app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")',
     'app = FastAPI(title="rootend")', True, 1),
]

SYSTEM_TOOLS_PY = [
    ("docstring", r"^system_tools\.py - [^\n]*$",
     "system_tools.py - rootend: کارهایی که با سیستم‌عامل کاربر سروکار دارند",
     "system_tools.py - rootend", True, 1),
    ("ipc names", r"courseplanner-", "rootend-", "rootend-", True, 0),
]

COURSE_TIMER_PY = [
    ("docstring", r"(?m)^course_timer\.py\n=+$",
     "course_timer.py - rootend (root + tend: مراقبت از ریشه‌ها)\n"
     "==========================================================",
     "course_timer.py - rootend", True, 1),
    ("cli description", r'description="Calculate the total duration of a video course, "',
     'description="rootend - calculate the total duration of a video course, "',
     'description="rootend - calculate', True, 1),
]

INDEX_HTML = [
    ("title + favicon", r"<title>[^<]*</title>",
     "<title>rootend</title>\n"
     f'<link rel="icon" type="image/svg+xml" href="{LOGO_SVG}">\n'
     f'<link rel="icon" type="image/jpeg" href="{LOGO_JPG}">\n'
     f'<link rel="apple-touch-icon" href="{LOGO_JPG}">',
     "<title>rootend</title>", True, 1),
    ("logo css", r"  \.brand \{ color: inherit; text-decoration: none; \}",
     "  .brand { color: inherit; text-decoration: none; display: inline-flex; align-items: center; gap: 10px; }\n"
     "  .logo { display: block; height: 44px; width: auto; max-width: 100%; border-radius: 8px; }\n"
     "  .empty-logo { height: 72px; margin: 0 auto 6px; }\n"
     "  /* اگر لوگوی شما فقط آیکون است و نوشته ندارد، در :root بنویسید:  --brand-text: inline-block; */\n"
     "  .brand-text { display: var(--brand-text, none); font-weight: 800; letter-spacing: .5px; }\n"
     "  /* لوگو در حالت تاریک روی زمینه‌ی روشن می‌نشیند تا با هر رنگی خوانا بماند */\n"
     "  @media (prefers-color-scheme: dark) { .logo { background: #fff; padding: 4px 8px; } }",
     ".brand-text {", True, 1),
    ("brand markup", r'<h1><a class="brand" href="#/programs">[^<]*</a></h1>',
     '<h1><a class="brand" href="#/programs" title="rootend — مراقبت از ریشه‌ها">\n'
     f'    <img class="logo" src="{LOGO_SVG}" alt="rootend"\n'
     f"         onerror=\"this.onerror=null;this.src='{LOGO_JPG}'\">\n"
     '    <span class="brand-text">rootend</span>\n'
     '  </a></h1>',
     'class="logo"', True, 1),
    ("document.title (list)", r"(?m)^(\s*)document\.title = '[^'\n]*';$",
     r"\g<1>document.title = 'rootend';", "document.title = 'rootend';", True, 1),
    ("document.title (program)", r"(document\.title = `\$\{state\.prog\.name\})[^`\n]*`;",
     r"\g<1> — rootend`;", " — rootend`;", True, 1),
    ("empty-state logo", r"box\.append\(el\('div', \{ class: 'card empty-card' \},",
     "box.append(el('div', { class: 'card empty-card' },\n"
     f"      el('img', {{ class: 'logo empty-logo', src: '{LOGO_SVG}', alt: '' }}),",
     "class: 'logo empty-logo'", True, 1),
]

FILES = [("app.py", APP_PY), ("system_tools.py", SYSTEM_TOOLS_PY),
         ("course_timer.py", COURSE_TIMER_PY), ("static/index.html", INDEX_HTML)]


def patch_text(text, edits):
    """(متن جدید، گزارش، آیا خطا بود)"""
    report, failed = [], False
    for label, pattern, repl, marker, fatal, count in edits:
        if marker and marker in text:
            report.append(f"    = {label}: قبلاً انجام شده")
            continue
        new, n = re.subn(pattern, lambda m, r=repl: m.expand(r), text, count=count, flags=re.M)
        if n:
            text = new
            report.append(f"    + {label}")
        elif fatal:
            report.append(f"    ! {label}: خط مورد نظر پیدا نشد")
            failed = True
        else:
            report.append(f"    - {label}: (اختیاری) پیدا نشد؛ رد شد")
    return text, report, failed


def main(argv):
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    print(f"پوشه‌ی پروژه: {root}\n")
    pending, any_failed = {}, False
    for rel, edits in FILES:
        path = root / rel
        if not path.is_file():
            print(f"{rel}: فایل پیدا نشد")
            any_failed = True
            continue
        original = path.read_text(encoding="utf-8")
        new, report, failed = patch_text(original, edits)
        print(f"{rel}:" + ("   <- هیچ تغییری نوشته نمی‌شود" if failed else ""))
        print("\n".join(report))
        if failed:
            any_failed = True
        elif new != original:
            pending[path] = new

    if any_failed:
        print("\nبه‌خاطر خطاهای بالا هیچ فایلی تغییر نکرد. فایل‌هایی که خطا دارند را برایم بفرستید.")
        return 1
    for path, new in pending.items():
        path.write_text(new, encoding="utf-8")
    print(f"\n{len(pending)} فایل به‌روز شد." if pending else "\nهمه‌چیز از قبل انجام شده بود.")

    images = root / "assets" / "images"
    for name in ("rootend_logo_accurate.svg", "rootend_logo_accurate.jpg"):
        if not (images / name).is_file():
            print(f"هشدار: {images / name} پیدا نشد؛ لوگو نمایش داده نمی‌شود.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))