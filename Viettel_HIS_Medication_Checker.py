# -*- coding: utf-8 -*-
# AUTOUPDATE TEST A5 - harmless version bump for updater validation
"""
Viettel HIS Medication Checker - V82.6A5 FIX RESTART AUTOUPDATE GITHUB / MAX SPEED
--------------------------------------------------------------
Mục tiêu:
- Giữ nguyên logic kiểm tra thuốc của V79.
- Dùng 1 UIAutomation COM worker cố định thay vì tạo thread COM mới mỗi scan.
- Tự reset UIAutomation khi COM/UIA bị lỗi.
- Không để exception UIA bị nuốt im lặng.
- Có watchdog + log.
- Không polling UIAutomation liên tục.
- Giữ F2/F4/click trigger và Tray.
- Sửa lỗi check_row() bị chạy 2 lần.
"""

import sys
import os
import re
import time
import ctypes
import threading
import queue
import warnings
import logging
import json
import hashlib
import urllib.request
import urllib.error
import subprocess
import tempfile
import datetime
from logging.handlers import RotatingFileHandler
import tkinter as tk

warnings.simplefilter("ignore", category=UserWarning)

# ============================================================
# WINDOWS / COM
# ============================================================

# 0 = COINIT_MULTITHREADED
sys.coinit_flags = 0

VK_F2 = 0x71
VK_F4 = 0x73
VK_LBUTTON = 0x01

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

POPUP_DURATION = 300

# ============================================================
# AUTO UPDATE - GITHUB RAW
# ============================================================

APP_VERSION = "82.6A5"
APP_VERSION_CODE = 82605

# SAU KHI TẠO GITHUB, CHỈ CẦN SỬA 2 DÒNG NÀY 1 LẦN:
GITHUB_USER = "lucastonybn"
GITHUB_REPO = "viettel-his-checker"
GITHUB_BRANCH = "main"

UPDATE_MANIFEST_URL = (
    f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/"
    f"{GITHUB_BRANCH}/version.json"
)

UPDATE_CHECK_HOUR = 1   # 01:00 mỗi ngày
UPDATE_TIMEOUT = 12
UPDATE_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(sys.argv[0])),
    ".update_state.json"
)

_update_lock = threading.Lock()


# ============================================================
# PATH / LOG
# ============================================================

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
LOG_DIR = os.path.join(APP_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

LOG_FILE = os.path.join(LOG_DIR, "checker.log")

logger = logging.getLogger("HISChecker")
logger.setLevel(logging.INFO)
logger.propagate = False

if not logger.handlers:
    handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8"
    )
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        "%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

# ============================================================
# ADMIN
# ============================================================

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

if not is_admin():
    script_path = os.path.abspath(sys.argv[0])

    # Nếu chạy .py thì:
    # ShellExecuteW(exe, runas, python.exe, '"C:\\path\\file.py"', ...)
    # Nếu chạy EXE thì sys.executable chính là EXE.
    if script_path.lower().endswith(".py"):
        params = f'"{script_path}"'
    else:
        params = ""

    ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        sys.executable,
        params,
        APP_DIR,
        1
    )
    sys.exit(0)

# ============================================================
# HIDE CONSOLE / UTF-8
# ============================================================

try:
    hwnd_console = kernel32.GetConsoleWindow()
    if hwnd_console:
        user32.ShowWindow(hwnd_console, 0)
except Exception:
    pass

try:
    os.system("chcp 65001 >nul 2>&1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ============================================================
# IMPORT COMTYPES
# ============================================================

try:
    import comtypes
    import comtypes.client
    UIAutomationClient = comtypes.client.GetModule("UIAutomationCore.dll")
except ImportError:
    print("[!] Thiếu comtypes.")
    print("[!] Cài bằng:")
    print("    python -m pip install comtypes")
    sys.exit(1)
except Exception as e:
    print("[!] Không load được UIAutomationCore.dll:", e)
    sys.exit(1)

# ============================================================
# AUTO UPDATE ENGINE
# ============================================================

def _show_info(title, message):
    """Hiện thông báo an toàn từ Tk main thread nếu có thể."""
    try:
        from tkinter import messagebox
        if app_tk_root:
            app_tk_root.after(0, lambda: messagebox.showinfo(title, message))
            return
    except Exception:
        pass


def _show_error(title, message):
    try:
        from tkinter import messagebox
        if app_tk_root:
            app_tk_root.after(0, lambda: messagebox.showerror(title, message))
            return
    except Exception:
        pass


def _http_get_bytes(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Viettel-HIS-Checker-Updater/1.0",
            "Cache-Control": "no-cache",
        }
    )
    with urllib.request.urlopen(req, timeout=UPDATE_TIMEOUT) as resp:
        return resp.read()


def _load_update_state():
    try:
        with open(UPDATE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_update_state(data):
    try:
        tmp = UPDATE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, UPDATE_STATE_FILE)
    except Exception as e:
        logger.warning("UPDATE_STATE_SAVE_FAILED | %s", e)


def _validate_manifest(m):
    if not isinstance(m, dict):
        raise ValueError("version.json không hợp lệ")

    version = str(m.get("version", "")).strip()
    version_code = int(m.get("version_code", 0))
    url = str(m.get("url", "")).strip()
    sha256 = str(m.get("sha256", "")).strip().lower()

    if not version or version_code <= 0 or not url:
        raise ValueError("version.json thiếu version/version_code/url")

    if not url.startswith("https://raw.githubusercontent.com/"):
        raise ValueError("URL cập nhật phải là GitHub Raw HTTPS")

    if sha256 and not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("sha256 không hợp lệ")

    return version, version_code, url, sha256


def _write_self_replace_batch(new_file, current_file):
    """
    Tạo file .cmd tạm nhiều dòng.
    Win7/Win10 xử lý label/goto ổn định hơn nhiều so với CMD /c một dòng.
    """
    pid = os.getpid()
    backup = current_file + ".bak"
    pyexe = sys.executable

    temp_dir = os.environ.get("TEMP") or os.path.dirname(current_file)
    batch_file = os.path.join(
        temp_dir,
        f"viettel_his_update_{pid}.cmd"
    )

    batch_text = f"""@echo off
setlocal
:wait_old
tasklist /FI "PID eq {pid}" 2>NUL | find "{pid}" >NUL
if not errorlevel 1 (
    ping 127.0.0.1 -n 2 >NUL
    goto wait_old
)

copy /Y "{current_file}" "{backup}" >NUL 2>&1
if errorlevel 1 goto failed

move /Y "{new_file}" "{current_file}" >NUL 2>&1
if errorlevel 1 goto failed

start "" "{pyexe}" "{current_file}"
goto cleanup

:failed
start "" "{pyexe}" "{current_file}"

:cleanup
del "%~f0"
"""

    with open(batch_file, "w", encoding="mbcs", errors="replace") as f:
        f.write(batch_text)

    return batch_file


def _install_downloaded_update(new_file, version):
    current_file = os.path.abspath(sys.argv[0])

    if not current_file.lower().endswith(".py"):
        raise RuntimeError("Bản auto-update hiện tại hỗ trợ file .py")

    batch_file = _write_self_replace_batch(
        new_file,
        current_file
    )

    logger.info(
        "UPDATE_INSTALL_READY | current=%s | new=%s | batch=%s | version=%s",
        current_file,
        new_file,
        batch_file,
        version
    )

    # Chạy helper batch độc lập. Nó sẽ đợi process hiện tại thoát,
    # thay file rồi mở lại checker.
    subprocess.Popen(
        ["cmd.exe", "/c", batch_file],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True
    )

    # Thoát Tk mainloop một cách dứt khoát.
    try:
        shutdown_event.set()

        if app_tk_root:
            app_tk_root.after(0, app_tk_root.quit)
        else:
            raise SystemExit
    except Exception:
        try:
            if app_tk_root:
                app_tk_root.quit()
        except Exception:
            pass


def check_for_update(manual=False):
    """Kiểm tra GitHub Raw. manual=True thì có thông báo kết quả cho người dùng."""
    if not _update_lock.acquire(False):
        if manual:
            _show_info("Cập nhật", "Đang có một lần kiểm tra cập nhật khác chạy.")
        return

    try:
        if "DOI_TEN_GITHUB_CUA_BAN" in GITHUB_USER:
            msg = "Chưa cấu hình GITHUB_USER trong file checker."
            logger.warning("UPDATE_NOT_CONFIGURED")
            if manual:
                _show_error("Cập nhật", msg)
            return

        logger.info("UPDATE_CHECK_START | manual=%s | url=%s", manual, UPDATE_MANIFEST_URL)

        raw = _http_get_bytes(UPDATE_MANIFEST_URL)
        manifest = json.loads(raw.decode("utf-8-sig"))
        version, version_code, download_url, expected_sha = _validate_manifest(manifest)

        state = _load_update_state()
        state["last_check"] = datetime.datetime.now().isoformat(timespec="seconds")
        state["last_seen_version"] = version
        _save_update_state(state)

        logger.info(
            "UPDATE_REMOTE | current=%s(%s) | remote=%s(%s)",
            APP_VERSION, APP_VERSION_CODE, version, version_code
        )

        if version_code <= APP_VERSION_CODE:
            logger.info("UPDATE_NONE | latest=%s", version)
            if manual:
                _show_info(
                    "Cập nhật",
                    f"Đang dùng bản mới nhất: {APP_VERSION}"
                )
            return

        logger.info("UPDATE_DOWNLOAD_START | version=%s | url=%s", version, download_url)
        new_bytes = _http_get_bytes(download_url)

        # Bảo vệ cơ bản: file mới phải là Python checker, không phải HTML lỗi.
        if len(new_bytes) < 5000 or b"Viettel HIS Medication Checker" not in new_bytes[:12000]:
            raise RuntimeError("File tải về không giống checker hợp lệ")

        actual_sha = hashlib.sha256(new_bytes).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            raise RuntimeError(
                "SHA256 không khớp: file tải về có thể lỗi hoặc chưa đồng bộ"
            )

        current_file = os.path.abspath(sys.argv[0])
        new_file = current_file + ".update_new.py"

        with open(new_file, "wb") as f:
            f.write(new_bytes)

        # Compile trước khi thay file để tránh đẩy xuống code syntax-error.
        try:
            compile(new_bytes.decode("utf-8-sig"), new_file, "exec")
        except Exception as e:
            try:
                os.remove(new_file)
            except Exception:
                pass
            raise RuntimeError(f"Bản mới lỗi cú pháp Python: {e}")

        logger.info(
            "UPDATE_DOWNLOADED | version=%s | bytes=%s | sha256=%s",
            version,
            len(new_bytes),
            actual_sha
        )

        if manual:
            _show_info(
                "Cập nhật",
                f"Đã tải bản {version}. Phần mềm sẽ tự khởi động lại."
            )
            time.sleep(0.8)

        _install_downloaded_update(new_file, version)

    except urllib.error.HTTPError as e:
        logger.exception("UPDATE_HTTP_ERROR | %s", e)
        if manual:
            _show_error("Cập nhật", f"Không tải được bản cập nhật. HTTP {e.code}")
    except urllib.error.URLError as e:
        logger.exception("UPDATE_NETWORK_ERROR | %s", e)
        if manual:
            _show_error("Cập nhật", "Không kết nối được GitHub. Checker vẫn chạy bình thường.")
    except Exception as e:
        logger.exception("UPDATE_ERROR | %s", e)
        if manual:
            _show_error("Cập nhật", f"Cập nhật thất bại: {e}")
    finally:
        _update_lock.release()


def update_now_async():
    threading.Thread(
        target=check_for_update,
        kwargs={"manual": True},
        name="HIS-ManualUpdate",
        daemon=True
    ).start()


def daily_update_loop():
    """
    Mỗi ngày, sau 01:00 nếu ngày đó chưa check thì check một lần.
    Nếu máy tắt lúc 01:00 và bật 07:00 thì 07:00 sẽ check ngay.
    """
    while not shutdown_event.is_set():
        try:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            state = _load_update_state()
            last_day = state.get("last_auto_check_day", "")

            if now.hour >= UPDATE_CHECK_HOUR and last_day != today:
                # Đánh dấu trước để tránh lặp liên tục nếu mạng đang lỗi.
                state["last_auto_check_day"] = today
                _save_update_state(state)

                check_for_update(manual=False)

            shutdown_event.wait(60.0)
        except Exception as e:
            logger.exception("UPDATE_LOOP_ERROR | %s", e)
            shutdown_event.wait(60.0)

# ============================================================
# OPTIONAL TRAY
# ============================================================

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# ============================================================
# MEDICATION LOGIC - GIỮ NGUYÊN V79
# ============================================================

MED_UNITS = {
    "viên", "vien", "v", "lọ", "lo", "chai", "ống", "ong",
    "tuýp", "tuyp", "gói", "goi", "túi", "tui", "bột", "bot",
    "gam", "g", "mg", "mcg", "kg", "ml", "lit", "lít",
    "thang", "liều", "lieu", "bơm", "bom", "vỉ", "vi", "hộp",
    "hop", "bịch", "bich"
}

UNIT_ALIASES = {
    "vien": "viên", "viên": "viên", "v": "viên",
    "lo": "lọ", "lọ": "lọ",
    "chai": "chai",
    "ong": "ống", "ống": "ống",
    "ml": "ml",
    "tuyp": "tuýp", "tuýp": "tuýp",
    "goi": "gói", "gói": "gói",
    "tui": "túi", "túi": "túi",
    "bot": "bột", "bột": "bột",
    "gam": "g", "g": "g",
    "mg": "mg", "mcg": "mcg", "kg": "kg",
    "thang": "thang", "thang thuốc": "thang"
}

IGNORE_KEYWORDS = {
    "gói sinh", "gói đẻ", "gói mổ", "gói khám", "gói dv",
    "gói dịch vụ", "gói phẫu thuật", "gói thủ thuật",
    "gói điều trị", "gói thai sản",
    "thành tiền", "đơn giá", "liều dùng", "cách dùng",
    "khoản tt", "mã kcb", "số ba"
}

def norm_text(s):
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s).replace("\xa0", " ").strip())

def parse_number(s):
    if s is None:
        return None

    s = str(s).strip().replace(" ", "")
    if not s:
        return None

    if "/" in s:
        parts = s.split("/")
        if len(parts) == 2:
            try:
                return (
                    float(parts[0].replace(",", "."))
                    / float(parts[1].replace(",", "."))
                )
            except Exception:
                pass

    if "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")

    try:
        return float(s)
    except Exception:
        return None

def fmt_num(x):
    if x is None:
        return "?"
    return (
        str(int(round(x)))
        if abs(x - round(x)) < 1e-9
        else f"{x:.4f}".rstrip("0").rstrip(".")
    )

def normalize_unit(u):
    if not u:
        return ""

    u_clean = norm_text(u).lower().replace(".", "")
    return UNIT_ALIASES.get(u_clean, u_clean)

def is_medication_by_unit(unit_raw):
    if not unit_raw:
        return False

    u_lower = norm_text(unit_raw).lower()

    if any(kw in u_lower for kw in IGNORE_KEYWORDS):
        return False

    u = normalize_unit(unit_raw)

    if not u:
        return False

    parts = u.split()
    if not parts:
        return False

    return parts[0] in MED_UNITS

def clean_dose_text(raw):
    t = norm_text(raw)

    t = re.sub(
        r"[\[\(]\s*tự\s*túc\s*[\]\)]",
        "",
        t,
        flags=re.I
    )

    t = re.sub(
        r"[\[\(]\s*tu\s*tuc\s*[\]\)]",
        "",
        t,
        flags=re.I
    )

    return re.sub(r"^\d+\s*\|\s*", "", t).strip()

def parse_dose_robust(raw_text):
    clean_t = clean_dose_text(raw_text)

    res = {
        "raw": raw_text,
        "dose_per_time": None,
        "dose_unit": None,
        "times_per_day": None,
        "days": 1.0,
        "calc_total": None,
        "calc_unit": None,
        "exp_meds_per_day": None,
        "exp_unit": None,
        "notes": []
    }

    if not clean_t:
        return res

    low = clean_t.lower()

    outside_brackets = re.sub(
        r"\[[^\]]*\]|\([^\)]*\)",
        " ",
        low
    )

    m_days = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:ngày|ngay)\b",
        outside_brackets
    )

    res["days"] = parse_number(m_days.group(1)) if m_days else 1.0

    m_exp = re.search(
        r"[\[\(](\d+(?:[.,]\d+)?)\s*([a-zA-ZÀ-ỹ]+)?\s*/\s*"
        r"(?:ngày|ngay)[\]\)]",
        low
    )

    if m_exp:
        res["exp_meds_per_day"] = parse_number(m_exp.group(1))
        res["exp_unit"] = (
            normalize_unit(m_exp.group(2))
            if m_exp.group(2)
            else None
        )

    m_per_time = re.search(
        r"(\d+\s*/\s*\d+|\d+(?:[.,]\d+)?)\s*"
        r"([a-zA-ZÀ-ỹ]+)\s*/\s*(?:lần|lan)\b",
        low
    )

    if m_per_time:
        res["dose_per_time"] = parse_number(m_per_time.group(1))
        res["dose_unit"] = normalize_unit(m_per_time.group(2))

    m_times = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:lần|lan)\s*/\s*(?:ngày|ngay)\b",
        low
    )

    if m_times:
        res["times_per_day"] = parse_number(m_times.group(1))

    if (
        res["dose_per_time"] is not None
        and res["times_per_day"] is not None
    ):
        per_day = res["dose_per_time"] * res["times_per_day"]

        res["calc_total"] = per_day * res["days"]
        res["calc_unit"] = res["dose_unit"]

        if (
            res["exp_meds_per_day"] is not None
            and abs(res["exp_meds_per_day"] - per_day) > 1e-9
        ):
            res["notes"].append(
                f"Mâu thuẫn: Liều {fmt_num(per_day)} "
                f"≠ Ngoặc {fmt_num(res['exp_meds_per_day'])}"
            )

    elif res["exp_meds_per_day"] is not None:
        res["calc_total"] = (
            res["exp_meds_per_day"] * res["days"]
        )
        res["calc_unit"] = res["exp_unit"]

    return res

def parse_usage_time_points(usage_text):
    """
    Chỉ đọc Cách dùng.
    Nhận:
      - 8h, 8h30, 08:30, 9 giờ
      - sáng, trưa, chiều, tối, đêm
    Mỗi từ thời điểm chỉ tính 1 mốc.
    """
    if not usage_text:
        return 0, []

    text = norm_text(usage_text).lower()
    time_points = []

    # 8h, 8h30, 08:30, 9 giờ...
    for m in re.finditer(
        r"\b(\d{1,2})\s*(?:h|:|g|giờ)\s*(\d{1,2})?\b",
        text,
        flags=re.I
    ):
        hour = int(m.group(1))
        minute = m.group(2)

        if not (0 <= hour <= 24):
            continue

        point = f"{hour:02d}:{int(minute):02d}" if minute else f"{hour:02d}:00"
        if point not in time_points:
            time_points.append(point)

    # Các thời điểm bằng chữ.
    for word in ("sáng", "trưa", "chiều", "tối", "đêm"):
        if re.search(rf"\b{word}\b", text, flags=re.I) and word not in time_points:
            time_points.append(word)

    return len(time_points), time_points


def check_row(row):
    dvt_raw = row.get("unit", "")

    # Chỉ đối chiếu thuốc.
    if not is_medication_by_unit(dvt_raw):
        return {
            "status": "OK",
            "row": row,
            "errors": []
        }

    # QUAN TRỌNG V81:
    # - Liều dùng chỉ dùng để tính liều / số lượng / DVT.
    # - Cách dùng chỉ dùng để đếm thời điểm sử dụng.
    dose_raw = norm_text(row.get("dose", ""))
    usage_raw = norm_text(row.get("usage", ""))

    dose = parse_dose_robust(dose_raw)
    dvt = normalize_unit(dvt_raw)
    qty = parse_number(row.get("qty", ""))
    errors = []

    # -------------------------
    # 1. KIỂM TRA LIỀU DÙNG
    # -------------------------
    if not dose_raw or dose["calc_total"] is None:
        snippet = (
            dose_raw[:25] + "..."
            if len(dose_raw) > 25
            else dose_raw
        )
        errors.append("Sai cấu trúc liều")
    else:
        if (
            dvt
            and dose["dose_unit"]
            and dose["dose_unit"] not in {"buổi", "thang"}
            and dvt != dose["dose_unit"]
        ):
            errors.append(
                f"Lệch ĐVT ({dvt} ≠ {dose['dose_unit']})"
            )

        if (
            dvt
            and dose["exp_unit"]
            and dose["exp_unit"] not in {"buổi", "thang"}
            and dvt != dose["exp_unit"]
        ):
            errors.append(
                f"Lệch ĐVT ({dvt} ≠ {dose['exp_unit']})"
            )

        need = dose["calc_total"]

        if (
            qty is not None
            and need is not None
            and abs(qty - need) > 1e-9
        ):
            errors.append(
                f"Lệch SL: Kê {fmt_num(qty)} "
                f"→ Cần {fmt_num(need)}"
            )

    # -------------------------
    # 2. KIỂM TRA CÁCH DÙNG
    # -------------------------
    # Chỉ quan tâm SỐ MỐC THỜI ĐIỂM trong Cách dùng.
    # Tuyệt đối không lấy giờ từ Liều dùng.
    req_times = (
        int(dose["times_per_day"])
        if dose["times_per_day"] is not None
        else 1
    )

    count_points, points_found = parse_usage_time_points(usage_raw)

    if count_points == 0:
        errors.append("Thiếu thời điểm trong Cách dùng")

    elif count_points < req_times:
        errors.append(
            f"Thiếu thời điểm: Liều {req_times} lần/ngày "
            f"≠ Cách dùng {count_points} mốc"
        )

    elif count_points > req_times:
        errors.append(
            f"Thừa thời điểm: Liều {req_times} lần/ngày "
            f"≠ Cách dùng {count_points} mốc"
        )

    errors.extend(dose["notes"])

    return {
        "status": "ERROR" if errors else "OK",
        "row": row,
        "dose": dose,
        "errors": errors
    }



# ============================================================
# ACTIVE SCREEN GATE - V82.2
# ============================================================

ALLOWED_MEDICATION_SCREENS = (
    "TỜ ĐIỀU TRỊ",
    "KÊ ĐƠN NGOẠI TRÚ BHYT",
    "KÊ ĐƠN NGOẠI TRÚ",
)

# Các cửa sổ nghiệp vụ con phải chặn tuyệt đối, không dùng grace.
HARD_BLOCK_SCREEN_KEYWORDS = (
    "XUẤT GÓI",
    "XUAT GOI",
    "XUẤT GÓI THUỐC",
    "XUẤT GÓI DỊCH VỤ",
    "XUẤT GÓI ĐIỀU CHỈNH",
    "XUẤT TỪ TRỰC",
    "XUAT TU TRUC",
)

# HIS có thể đổi focus/title thoáng qua khi bấm F2/Lưu hoặc mở control con.
# Giữ "vé hợp lệ" ngắn để scan vẫn chạy sau trigger đúng màn hình.
ACTIVE_SCREEN_GRACE_SECONDS = 3.0
_last_allowed_screen_time = 0.0
_last_allowed_screen_title = ""

def _get_top_level_window(hwnd):
    try:
        GA_ROOT = 2
        root = user32.GetAncestor(hwnd, GA_ROOT)
        return root or hwnd
    except Exception:
        return hwnd

def get_foreground_window_title():
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return "", 0

        hwnd = _get_top_level_window(hwnd)

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return "", hwnd

        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return norm_text(buf.value), hwnd
    except Exception:
        return "", 0

def is_exact_allowed_medication_screen_active():
    """
    V82.6A2:
    Chấp nhận cả title đầy đủ của HIS và title rút gọn như
    "Tờ điều trị" / "Kê Đơn Ngoại Trú".
    """
    title, hwnd = get_foreground_window_title()
    if not hwnd or not title:
        return False

    upper = norm_text(title).upper()

    return any(
        screen == upper or screen in upper
        for screen in ALLOWED_MEDICATION_SCREENS
    )


def is_hard_block_screen_active():
    """
    Nếu foreground là cửa sổ nghiệp vụ con như Xuất gói thì chặn ngay.
    Không dùng grace trong trường hợp này.
    """
    title, hwnd = get_foreground_window_title()
    if not hwnd or not title:
        return False

    upper = title.upper()
    return any(k in upper for k in HARD_BLOCK_SCREEN_KEYWORDS)


def is_allowed_medication_screen_active(use_grace=True):
    """
    V82.6A2 TITLE FIX:
    - Nhận "Tờ điều trị" kể cả khi title chỉ có đúng cụm này.
    - Nhận "Kê Đơn Ngoại Trú" / "... BHYT".
    - "Thông báo" và màn khác vẫn bị chặn.
    """
    global _last_allowed_screen_time, _last_allowed_screen_title

    title, hwnd = get_foreground_window_title()
    now = time.monotonic()

    if hwnd and title:
        upper = norm_text(title).upper()

        # Hard-block luôn ưu tiên chặn.
        if any(k in upper for k in HARD_BLOCK_SCREEN_KEYWORDS):
            return False

        matched = any(
            screen == upper or screen in upper
            for screen in ALLOWED_MEDICATION_SCREENS
        )

        if matched:
            _last_allowed_screen_time = now
            _last_allowed_screen_title = title
            return True

    # Grace chỉ để chống việc HIS đổi focus/title thoáng qua.
    if use_grace and (now - _last_allowed_screen_time) <= ACTIVE_SCREEN_GRACE_SECONDS:
        return True

    return False

def hide_toast_if_needed():
    if is_allowed_medication_screen_active():
        return

    if app_tk_root and toast_mgr:
        try:
            app_tk_root.after(0, toast_mgr.hide_toast)
        except Exception:
            pass

# ============================================================
# WIN32 FIND HIS WINDOW
# ============================================================

HIS_TITLE_KEYWORDS = (
    "VIETTEL-HIS",
    "TỜ ĐIỀU TRỊ",
    "KHÁM BỆNH",
    "KÊ ĐƠN",
    "HỘI CHẨN"
)

def find_his_hwnd_win32():
    found = 0

    EnumProc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p
    )

    def enum_cb(h, _):
        nonlocal found

        try:
            if not user32.IsWindowVisible(h):
                return True

            length = user32.GetWindowTextLengthW(h)

            if length <= 0:
                return True

            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(h, buf, length + 1)

            title = buf.value.upper()

            if any(k in title for k in HIS_TITLE_KEYWORDS):
                found = h
                return False

        except Exception:
            pass

        return True

    try:
        user32.EnumWindows(EnumProc(enum_cb), 0)
    except Exception as e:
        logger.exception("find_his_hwnd_win32 | %s", e)

    return found

# ============================================================
# UIAUTOMATION EXCEPTIONS
# ============================================================

def is_uia_recoverable_error(exc):
    text = f"{type(exc).__name__}: {exc}".lower()

    keywords = (
        "elementnotavailable",
        "timeout",
        "uia_e_",
        "rpc_e_",
        "comerror",
        "call was rejected",
        "server execution failed",
        "object has disconnected",
        "the object invoked has disconnected"
    )

    return any(k in text for k in keywords)

# ============================================================
# COLUMN POSITION HELPERS - V81
# ============================================================

def _uia_find_header_x(uia, root, names):
    """Trả về X trái của header đầu tiên tìm thấy; -9999 nếu không có."""
    for header_name in names:
        try:
            cond = uia.CreatePropertyCondition(30005, header_name)
            el = root.FindFirst(4, cond)
            if el:
                rect = el.GetCurrentPropertyValue(30001)
                if rect and len(rect) == 4:
                    return rect[0]
        except Exception:
            pass
    return -9999

# ============================================================
# EXACT NAMED CELL PARSER - V82
# ============================================================

CELL_ROW_RE = re.compile(r"\bRow\s+(\d+)\b", flags=re.I)


def _field_from_uia_cell_name(name):
    """Map đúng cell DataGridView do HIS expose qua UI Automation."""
    low = norm_text(name).lower()

    if "row " not in low:
        return None, None

    m = CELL_ROW_RE.search(name)
    if not m:
        return None, None

    row_no = int(m.group(1))

    # Thứ tự rất quan trọng: Cách dùng/Liều dùng phải tách riêng.
    if "tên thuốc, vật tư" in low or "tên thuốc vật tư" in low or "tên thuốc" in low:
        return row_no, "name"
    if re.search(r"(^|\s)đvt(\s|$)", low) or "đơn vị tính" in low:
        return row_no, "unit"
    if re.search(r"(^|\s)sl(\s|$)", low) or "số lượng" in low:
        return row_no, "qty"
    if "cách dùng" in low or "đường dùng" in low:
        return row_no, "usage"
    if "liều dùng" in low:
        return row_no, "dose"

    return None, None


def _uia_value_fast(el):
    """Đọc ValuePattern trước, không lấy Name làm nội dung cell."""
    # UIA ValueValuePropertyId
    try:
        v = el.GetCurrentPropertyValue(30045)
        if v is not None:
            s = norm_text(str(v))
            if s and s.lower() not in {"null", "none"}:
                return s
    except Exception:
        pass

    # LegacyIAccessible.Value
    try:
        v = el.GetCurrentPropertyValue(30093)
        if v is not None:
            s = norm_text(str(v))
            if s and s.lower() not in {"null", "none"}:
                return s
    except Exception:
        pass

    return ""

# ============================================================
# UIAUTOMATION CORE
# ============================================================

class UIAutomationCore:
    """
    Toàn bộ COM/UIAutomation chỉ chạy trên một worker thread.
    Không chia sẻ CUIAutomation/element sang Tkinter hoặc monitor thread.
    """

    def __init__(self):
        self.uia = None
        self.initialized = False

    def initialize(self):
        if self.uia is not None:
            return True

        try:
            self.uia = comtypes.client.CreateObject(
                UIAutomationClient.CUIAutomation,
                interface=UIAutomationClient.IUIAutomation
            )

            self.initialized = True
            logger.info("UIA_INIT | CUIAutomation created")
            return True

        except Exception as e:
            self.uia = None
            self.initialized = False

            logger.exception(
                "UIA_INIT_ERROR | %s",
                e
            )

            return False

    def reset(self, reason="unknown"):
        logger.warning(
            "UIA_RESET | reason=%s",
            reason
        )

        old = self.uia
        self.uia = None
        self.initialized = False

        try:
            del old
        except Exception:
            pass

        # Cho COM wrapper có cơ hội release reference.
        time.sleep(0.01)

        return self.initialize()

    def close(self):
        old = self.uia
        self.uia = None
        self.initialized = False

        try:
            del old
        except Exception:
            pass

    def extract_anti_ghost(self):
        """
        V82 NAMED-CELL FAST:
        - Không suy cột bằng tọa độ X nữa.
        - Đọc đúng các cell mà DataGridView expose như:
            Tên thuốc, vật tư Row 1
            DVT Row 1
            SL Row 1
            Cách dùng Row 1
            Liều dùng Row 1
        - ValuePattern của Cách dùng và Liều dùng được đọc riêng tuyệt đối.
        - Không đọc Đơn giá/Thành tiền/Phiếu lĩnh => ít COM calls hơn.
        """
        if not self.initialize():
            return []

        uia = self.uia
        hwnd = find_his_hwnd_win32()
        if not hwnd:
            logger.warning("SCAN | HIS_WINDOW_NOT_FOUND")
            return []

        win_elem = None
        header = None
        target_grid = None
        elements = None

        try:
            win_elem = uia.ElementFromHandle(hwnd)
            target_grid = win_elem

            # Tìm header thuốc để leo lên đúng DataGrid/Table.
            cond_h1 = uia.CreatePropertyCondition(30005, "Tên thuốc, vật tư")
            cond_h2 = uia.CreatePropertyCondition(30005, "Tên thuốc")
            cond_h3 = uia.CreatePropertyCondition(30005, "Tên thuốc vật tư")
            or_cond = uia.CreateOrCondition(cond_h1, cond_h2)
            cond_header = uia.CreateOrCondition(or_cond, cond_h3)
            header = win_elem.FindFirst(4, cond_header)

            if header:
                walker = uia.ControlViewWalker
                curr = header
                for _ in range(8):
                    parent = walker.GetParentElement(curr)
                    if not parent:
                        break
                    curr = parent
                    if curr.CurrentControlType in (50028, 50036):
                        target_grid = curr
                        break

            # Một FindAll duy nhất. Sau đó chỉ đọc CurrentName; chỉ 5 field cần thiết
            # mới gọi ValuePattern. Không gọi BoundingRectangle cho từng element.
            elements = target_grid.FindAll(4, uia.CreateTrueCondition())
            rows = {}

            for i in range(elements.Length):
                try:
                    el = elements.GetElement(i)

                    # Name là metadata cần để biết cell nào; rẻ hơn đọc rectangle + value mọi element.
                    try:
                        cell_name = norm_text(el.CurrentName)
                    except Exception:
                        cell_name = ""

                    if not cell_name or "row " not in cell_name.lower():
                        continue

                    row_no, field = _field_from_uia_cell_name(cell_name)
                    if row_no is None or field is None:
                        continue

                    value = _uia_value_fast(el)
                    rows.setdefault(row_no, {})

                    # Một số HIS có nested element cùng tên. Giữ giá trị dài/đầy đủ hơn.
                    old = rows[row_no].get(field, "")
                    if value and (not old or len(value) > len(old)):
                        rows[row_no][field] = value
                    elif field not in rows[row_no]:
                        rows[row_no][field] = ""

                except Exception as e:
                    if is_uia_recoverable_error(e):
                        raise
                    continue

            all_rows = []
            for row_no in sorted(rows):
                r = rows[row_no]
                name = norm_text(r.get("name", ""))
                if not name or name.lower() in {"null", "(null)", "none", "tên thuốc, vật tư"}:
                    continue

                # Chỉ thuốc; vật tư bị bỏ ngay từ đây để giảm logic phía sau.
                if not is_medication_by_unit(r.get("unit", "")):
                    continue

                r["_row_no"] = row_no
                all_rows.append(r)

                # Log ngắn để khi có false positive nhìn được chính xác 2 cột đã đọc.
                logger.info(
                    "ROW_DATA | row=%s | name=%s | unit=%s | qty=%s | usage=%s | dose=%s",
                    row_no,
                    name,
                    r.get("unit", ""),
                    r.get("qty", ""),
                    r.get("usage", ""),
                    r.get("dose", "")
                )

            return all_rows

        finally:
            try:
                del elements
            except Exception:
                pass
            try:
                del header
            except Exception:
                pass
            try:
                del target_grid
            except Exception:
                pass
            try:
                del win_elem
            except Exception:
                pass


# ============================================================
# TOAST
# ============================================================

class SafeToastManager:

    def __init__(self, master):
        self.master = master
        self.window = None
        self.after_id = None

    def update_toast(self, error_items):

        if self.after_id and self.window:
            try:
                self.window.after_cancel(
                    self.after_id
                )
            except Exception:
                pass

            self.after_id = None

        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

            self.window = None

        if not error_items:
            return

        self.window = tk.Toplevel(self.master)
        self.window.title("Cảnh báo")
        self.window.attributes("-topmost", True)
        self.window.overrideredirect(True)

        outer = tk.Frame(
            self.window,
            bg="#d32f2f",
            padx=2,
            pady=2
        )
        outer.pack(fill="both", expand=True)

        inner = tk.Frame(
            outer,
            bg="#fffde7",
            padx=8,
            pady=6
        )
        inner.pack(fill="both", expand=True)

        hf = tk.Frame(
            inner,
            bg="#fffde7"
        )
        hf.pack(fill="x", pady=(0, 2))

        tk.Label(
            hf,
            text=f"⚠ CẢNH BÁO ({len(error_items)} lỗi)",
            bg="#fffde7",
            fg="#b00000",
            font=("Segoe UI", 9, "bold")
        ).pack(side="left")

        btn = tk.Label(
            hf,
            text="✕",
            bg="#fffde7",
            fg="#555",
            font=("Segoe UI", 9, "bold"),
            cursor="hand2"
        )

        btn.pack(side="right")
        btn.bind(
            "<Button-1>",
            lambda e: self.hide_toast()
        )

        for item in error_items:
            med_name = item["row"].get(
                "name",
                ""
            )

            if len(med_name) > 20:
                med_name = med_name[:18] + ".."

            tk.Label(
                inner,
                text=f"• {med_name}",
                bg="#fffde7",
                fg="#000",
                font=("Segoe UI", 8, "bold"),
                justify="left"
            ).pack(anchor="w")

            for err in item["errors"]:
                tk.Label(
                    inner,
                    text=f"  - {err}",
                    bg="#fffde7",
                    fg="#c62828",
                    font=("Segoe UI", 8),
                    justify="left"
                ).pack(anchor="w")

        self.window.update_idletasks()

        w = self.window.winfo_reqwidth()
        h = self.window.winfo_reqheight()

        sw = self.window.winfo_screenwidth()
        sh = self.window.winfo_screenheight()

        width = max(w, 240)

        self.window.geometry(
            f"{width}x{h}+"
            f"{sw - width - 15}+"
            f"{sh - h - 45}"
        )

        self.after_id = self.window.after(
            POPUP_DURATION * 1000,
            self.hide_toast
        )

    def hide_toast(self):

        if self.after_id and self.window:
            try:
                self.window.after_cancel(
                    self.after_id
                )
            except Exception:
                pass

            self.after_id = None

        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

            self.window = None

# ============================================================
# APPLICATION GLOBALS
# ============================================================

app_tk_root = None
toast_mgr = None

scan_queue = queue.Queue(maxsize=2)

shutdown_event = threading.Event()

worker_thread = None
worker_ready = threading.Event()

worker_state_lock = threading.Lock()

worker_state = {
    "status": "STARTING",
    "last_start": 0.0,
    "last_finish": 0.0,
    "last_duration_ms": 0.0,
    "last_error": "",
    "heartbeat": time.monotonic(),
    "scan_count": 0
}

# ============================================================
# WORKER STATE
# ============================================================

def set_worker_state(**kwargs):
    with worker_state_lock:
        worker_state.update(kwargs)
        worker_state["heartbeat"] = time.monotonic()

def get_worker_state():
    with worker_state_lock:
        return dict(worker_state)

# ============================================================
# SCAN
# ============================================================

def run_scan_once(uia_core, source):
    """
    Một scan hoàn chỉnh.
    Không gọi check_row hai lần.
    V82.2: kiểm tra lại active screen ngay trước lúc đọc UIA.
    """

    # V82.3:
    # Trigger đã được kiểm tra trước khi vào queue.
    # KHÔNG kiểm tra foreground lần hai ở đây vì ngay sau F2/Lưu
    # HIS thường đổi focus/title tạm thời, làm mất cảnh báo giả.
    t0 = time.perf_counter()

    set_worker_state(
        status="SCANNING",
        last_start=time.monotonic(),
        last_error=""
    )

    if app_tk_root:
        try:
            app_tk_root.after(
                0,
                toast_mgr.hide_toast
            )
        except Exception:
            pass

    try:
        all_rows = uia_core.extract_anti_ghost()

        # QUAN TRỌNG:
        # V79 gọi check_row() hai lần.
        # V80 chỉ gọi đúng một lần / row.
        results = [
            check_row(r)
            for r in all_rows
        ]

        error_items = [
            x
            for x in results
            if x["status"] == "ERROR"
        ]

        duration_ms = (
            time.perf_counter() - t0
        ) * 1000.0

        set_worker_state(
            status="HEALTHY",
            last_finish=time.monotonic(),
            last_duration_ms=duration_ms,
            scan_count=get_worker_state()["scan_count"] + 1
        )

        logger.info(
            "SCAN_OK | source=%s | rows=%d | errors=%d | %.2f ms",
            source,
            len(all_rows),
            len(error_items),
            duration_ms
        )

        if app_tk_root:
            try:
                app_tk_root.after(
                    0,
                    lambda errs=error_items:
                        toast_mgr.update_toast(errs)
                )
            except Exception as e:
                logger.exception(
                    "TOAST_UPDATE_ERROR | %s",
                    e
                )

        return True

    except Exception as e:
        duration_ms = (
            time.perf_counter() - t0
        ) * 1000.0

        set_worker_state(
            status="RECOVERING",
            last_finish=time.monotonic(),
            last_duration_ms=duration_ms,
            last_error=f"{type(e).__name__}: {e}"
        )

        logger.exception(
            "SCAN_ERROR | source=%s | %.2f ms | %s",
            source,
            duration_ms,
            e
        )

        return False

# ============================================================
# UI AUTOMATION WORKER
# ============================================================

class UIAutomationWorker(threading.Thread):

    def __init__(self):
        super().__init__(
            name="HIS-UIA-Worker",
            daemon=True
        )

        self.uia_core = None
        self.com_initialized = False

    def initialize_com(self):
        try:
            # Ưu tiên MTA.
            if hasattr(comtypes, "CoInitializeEx"):
                comtypes.CoInitializeEx(0)
            else:
                comtypes.CoInitialize()

            self.com_initialized = True

            logger.info(
                "COM_INIT | UIAutomation worker initialized"
            )

            return True

        except Exception as e:
            logger.exception(
                "COM_INIT_ERROR | %s",
                e
            )

            self.com_initialized = False
            return False

    def uninitialize_com(self):
        if not self.com_initialized:
            return

        try:
            comtypes.CoUninitialize()
        except Exception as e:
            logger.exception(
                "COM_UNINIT_ERROR | %s",
                e
            )

        self.com_initialized = False

    def recover_uia(self, reason):
        set_worker_state(
            status="RECOVERING",
            last_error=str(reason)
        )

        logger.warning(
            "RECOVERY_START | %s",
            reason
        )

        try:
            if self.uia_core:
                self.uia_core.reset(
                    reason=str(reason)
                )
            else:
                self.uia_core = UIAutomationCore()
                self.uia_core.initialize()

            logger.info(
                "RECOVERY_DONE | %s",
                reason
            )

        except Exception as e:
            logger.exception(
                "RECOVERY_ERROR | reason=%s | %s",
                reason,
                e
            )

    def run(self):

        if not self.initialize_com():
            set_worker_state(
                status="ERROR",
                last_error="COM initialization failed"
            )
            worker_ready.set()
            return

        try:
            self.uia_core = UIAutomationCore()

            if not self.uia_core.initialize():
                set_worker_state(
                    status="ERROR",
                    last_error="UIAutomation initialization failed"
                )

            worker_ready.set()

            set_worker_state(
                status="HEALTHY"
            )

            while not shutdown_event.is_set():

                try:
                    source = scan_queue.get(
                        timeout=0.25
                    )
                except queue.Empty:
                    set_worker_state(
                        status=get_worker_state()["status"]
                    )
                    continue

                if source is None:
                    break

                # Chống scan trùng do trigger dồn.
                # Chỉ xử lý một scan tại một thời điểm.
                ok = run_scan_once(
                    self.uia_core,
                    source
                )

                if not ok:
                    # Recovery ngay trong cùng COM worker.
                    self.recover_uia(
                        f"scan failed: {source}"
                    )

                    # Retry đúng 1 lần.
                    retry_ok = run_scan_once(
                        self.uia_core,
                        source + " [RETRY]"
                    )

                    if not retry_ok:
                        logger.error(
                            "SCAN_RETRY_FAILED | source=%s",
                            source
                        )

                        set_worker_state(
                            status="ERROR"
                        )

                scan_queue.task_done()

        except Exception as e:
            set_worker_state(
                status="ERROR",
                last_error=f"{type(e).__name__}: {e}"
            )

            logger.exception(
                "UIA_WORKER_FATAL | %s",
                e
            )

        finally:
            try:
                if self.uia_core:
                    self.uia_core.close()
            except Exception:
                pass

            self.uia_core = None

            self.uninitialize_com()

            set_worker_state(
                status="STOPPED"
            )

            logger.info(
                "UIA_WORKER_STOPPED"
            )

# ============================================================
# START WORKER
# ============================================================

def start_uia_worker():
    global worker_thread

    worker_ready.clear()

    worker_thread = UIAutomationWorker()
    worker_thread.start()

    # Không block lâu lúc khởi động.
    worker_ready.wait(timeout=3.0)

    if worker_thread.is_alive():
        logger.info(
            "UIA_WORKER_STARTED"
        )
    else:
        logger.error(
            "UIA_WORKER_START_FAILED"
        )

# ============================================================
# TRIGGER
# ============================================================

def trigger_scan(source, _transient_retry=0):

    if shutdown_event.is_set():
        return

    # V82.6A3:
    # Khi bấm XÁC NHẬN ở popup "Thông báo", foreground của HIS có thể
    # tạm thời có title="" hoặc "Thông báo" trong vài trăm ms.
    #
    # Không scan ngay trong trạng thái này vì có thể đọc sai cây UIA.
    # Chờ popup đóng rồi kiểm tra lại foreground thật.
    current_title, _ = get_foreground_window_title()
    current_upper = norm_text(current_title).upper()

    is_transient_popup_state = (
        not current_upper
        or current_upper == "THÔNG BÁO"
        or current_upper == "THONG BAO"
    )

    if is_transient_popup_state:
        if _transient_retry < 3:
            delay = 0.25

            logger.info(
                "TRIGGER_WAIT_TRANSIENT | source=%s | retry=%d | title=%s",
                source,
                _transient_retry + 1,
                current_title
            )

            timer = threading.Timer(
                delay,
                lambda: trigger_scan(
                    source,
                    _transient_retry + 1
                )
            )
            timer.daemon = True
            timer.start()
            return

        logger.info(
            "TRIGGER_SKIPPED_TRANSIENT_TIMEOUT | source=%s | title=%s",
            source,
            current_title
        )
        hide_toast_if_needed()
        return

    # Cửa sổ nghiệp vụ con như Xuất gói chặn tuyệt đối.
    if is_hard_block_screen_active():
        current_title, _ = get_foreground_window_title()
        logger.info(
            "TRIGGER_SKIPPED_HARD_BLOCK | source=%s | title=%s",
            source,
            current_title
        )
        hide_toast_if_needed()
        return

    # Giữ nguyên gate của V82.6A2 sau khi popup đã đóng.
    if not is_allowed_medication_screen_active(use_grace=True):
        current_title, _ = get_foreground_window_title()
        logger.info(
            "TRIGGER_SKIPPED_WRONG_SCREEN | source=%s | title=%s",
            source,
            current_title
        )
        hide_toast_if_needed()
        return

    # Không cho queue phình.
    try:
        scan_queue.put_nowait(source)

        logger.info(
            "TRIGGER | %s",
            source
        )

    except queue.Full:
        logger.info(
            "TRIGGER_IGNORED_QUEUE_FULL | %s",
            source
        )


# ============================================================
# WATCHDOG
# ============================================================

WATCHDOG_INTERVAL = 2.0
WATCHDOG_STALL_SECONDS = 8.0

def watchdog_loop():

    while not shutdown_event.is_set():

        try:
            time.sleep(WATCHDOG_INTERVAL)

            if shutdown_event.is_set():
                break

            wt = worker_thread

            if wt is None or not wt.is_alive():
                logger.error(
                    "WATCHDOG | worker thread is not alive"
                )

                # Chỉ khởi động worker mới khi worker cũ
                # thực sự đã chết.
                if not shutdown_event.is_set():
                    start_uia_worker()

                continue

            state = get_worker_state()

            if state["status"] == "SCANNING":
                elapsed = (
                    time.monotonic()
                    - state["last_start"]
                )

                if elapsed > WATCHDOG_STALL_SECONDS:
                    logger.error(
                        "WATCHDOG | possible stalled scan | %.2f sec",
                        elapsed
                    )

                    # Không tạo worker thứ hai ở đây.
                    # Python không thể an toàn kill thread đang nằm
                    # trong COM call. Chỉ ghi nhận để tránh tạo
                    # nhiều COM worker song song.
                    #
                    # Worker sẽ tự xử lý exception/recovery khi COM
                    # call trả về.
                    set_worker_state(
                        status="RECOVERING",
                        last_error=(
                            f"scan exceeded "
                            f"{WATCHDOG_STALL_SECONDS}s"
                        )
                    )

        except Exception as e:
            logger.exception(
                "WATCHDOG_ERROR | %s",
                e
            )

# ============================================================
# TRAY
# ============================================================

def create_tray_icon():

    if not HAS_TRAY:
        return

    try:
        image = Image.new(
            "RGBA",
            (64, 64),
            color=(255, 255, 255, 0)
        )

        draw = ImageDraw.Draw(image)

        draw.ellipse(
            (0, 0, 64, 64),
            fill=(41, 128, 185)
        )

        draw.rectangle(
            (26, 12, 38, 52),
            fill="white"
        )

        draw.rectangle(
            (12, 26, 52, 38),
            fill="white"
        )

        def on_quit(icon, item):
            shutdown()

            try:
                icon.stop()
            except Exception:
                pass

        def on_update(icon, item):
            logger.info("UPDATE_MANUAL_REQUEST")
            update_now_async()

        def on_version(icon, item):
            _show_info(
                "Viettel HIS Checker",
                f"Phiên bản hiện tại: {APP_VERSION}"
            )

        def on_open_log(icon, item):
            try:
                os.startfile(LOG_FILE)
            except Exception as e:
                logger.exception("OPEN_LOG_ERROR | %s", e)

        menu = pystray.Menu(
            pystray.MenuItem(
                "Kiểm tra cập nhật ngay",
                on_update
            ),
            pystray.MenuItem(
                f"Phiên bản: {APP_VERSION}",
                on_version
            ),
            pystray.MenuItem(
                "Mở log",
                on_open_log
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Thoát phần mềm (Exit)",
                on_quit
            )
        )

        icon = pystray.Icon(
            "HIS_Checker",
            image,
            "Viettel HIS Checker",
            menu
        )

        icon.run()

    except Exception as e:
        logger.exception(
            "TRAY_ERROR | %s",
            e
        )

# ============================================================
# MOUSE CLICK DETECTION
# ============================================================

CLICK_KEYWORDS = (
    "chấp nhận",
    "chap nhan",
    "đồng ý",
    "dong y",
    "lưu",
    "luu",
    "f2",
    "ok"
)

CLICK_IGNORE = (
    "cách dùng",
    "liều dùng",
    "xoá",
    "xóa",
    "xuất gói",
    "xuat goi",
    "xuất từ trực",
    "xuat tu truc"
)

def check_click_and_trigger():

    try:
        # V82.4: nếu đang ở cửa sổ Xuất gói/... thì bỏ ngay,
        # không tạo trigger và không làm popup nháy.
        if is_hard_block_screen_active():
            logger.info("CLICK_SKIPPED_HARD_BLOCK_SCREEN")
            hide_toast_if_needed()
            return
        pt = POINT()

        if not user32.GetCursorPos(
            ctypes.byref(pt)
        ):
            return

        hwnd = user32.WindowFromPoint(pt)

        if not hwnd:
            return

        length = user32.GetWindowTextLengthW(hwnd)

        if length <= 0:
            return

        buf = ctypes.create_unicode_buffer(
            length + 1
        )

        user32.GetWindowTextW(
            hwnd,
            buf,
            length + 1
        )

        name = buf.value.lower()

        if (
            any(k in name for k in CLICK_KEYWORDS)
            and not any(
                ign in name
                for ign in CLICK_IGNORE
            )
        ):
            # Chờ HIS cập nhật grid một khoảng rất ngắn.
            time.sleep(0.06)

            trigger_scan(
                "CLICK XÁC NHẬN"
            )

    except Exception as e:
        logger.exception(
            "CLICK_CHECK_ERROR | %s",
            e
        )

# ============================================================
# KEYBOARD / MOUSE MONITOR
# ============================================================

def monitor_actions():

    f2_down = False
    f4_down = False
    lb_down = False

    last_trig = 0.0

    while not shutdown_event.is_set():

        try:
            now = time.monotonic()

            # ----------------------------
            # F2
            # ----------------------------

            is_f2 = bool(
                user32.GetAsyncKeyState(VK_F2)
                & 0x8000
            )

            if (
                is_f2
                and not f2_down
                and now - last_trig > 0.5
            ):
                trigger_scan("F2 (LƯU)")
                last_trig = now

            f2_down = is_f2

            # ----------------------------
            # F4
            # ----------------------------

            is_f4 = bool(
                user32.GetAsyncKeyState(VK_F4)
                & 0x8000
            )

            if (
                is_f4
                and not f4_down
                and now - last_trig > 0.5
            ):
                trigger_scan("F4 (SỬA)")
                last_trig = now

            f4_down = is_f4

            # ----------------------------
            # LEFT CLICK
            # ----------------------------

            is_lb = bool(
                user32.GetAsyncKeyState(VK_LBUTTON)
                & 0x8000
            )

            if (
                is_lb
                and not lb_down
                and now - last_trig > 0.5
            ):
                threading.Thread(
                    target=check_click_and_trigger,
                    name="HIS-ClickCheck",
                    daemon=True
                ).start()

                last_trig = now

            lb_down = is_lb

            time.sleep(0.015)

        except Exception as e:
            logger.exception(
                "MONITOR_ERROR | %s",
                e
            )

            time.sleep(0.1)


# ============================================================
# ACTIVE SCREEN WATCHER - V82.2
# ============================================================

def active_screen_watcher():
    """
    V82.5:
    - Giữ cơ chế ẩn popup khi rời màn hình kê thuốc.
    - Ghi nhớ khi đã đi vào cửa sổ Xuất gói / nghiệp vụ hard-block.
    - Khi cửa sổ đó đóng và HIS quay lại Tờ điều trị / Kê đơn ngoại trú BHYT,
      tự scan 1 lần để bắt lỗi ngay, không cần click thêm.
    - Chỉ dùng Win32 để theo dõi; UIAutomation chỉ chạy khi thực sự trigger scan.
    """
    last_effective = None
    wrong_since = None
    was_in_hard_block = False
    return_rescan_done = False

    while not shutdown_event.is_set():
        try:
            now = time.monotonic()
            hard_block = is_hard_block_screen_active()
            exact_allowed = is_exact_allowed_medication_screen_active()
            allowed = is_allowed_medication_screen_active(use_grace=True)

            # Đã vào Xuất gói / cửa sổ nghiệp vụ con.
            if hard_block:
                if not was_in_hard_block:
                    logger.info("HARD_BLOCK_ENTERED")
                was_in_hard_block = True
                return_rescan_done = False

                # Ẩn ngay popup cũ nếu còn.
                hide_toast_if_needed()

            # Đã rời hard-block và quay lại đúng màn hình kê thuốc.
            elif was_in_hard_block and exact_allowed and not return_rescan_done:
                logger.info("RETURN_TO_MEDICATION_SCREEN | auto_rescan=scheduled")

                # Cho HIS ~250 ms dựng lại DataGrid sau khi modal đóng.
                time.sleep(0.25)

                # Kiểm tra lại một lần nữa để tránh scan nếu vừa chuyển tab tiếp.
                if is_exact_allowed_medication_screen_active():
                    trigger_scan("AUTO RETURN SAU XUẤT GÓI")
                    logger.info("RETURN_TO_MEDICATION_SCREEN | auto_rescan=triggered")
                    return_rescan_done = True
                    was_in_hard_block = False

            if allowed:
                wrong_since = None
                if last_effective is not True:
                    logger.info("ACTIVE_SCREEN_CHANGED | allowed=True")
                    last_effective = True
            else:
                if wrong_since is None:
                    wrong_since = now

                # Chỉ coi là rời thật sau 1 giây liên tục.
                if now - wrong_since >= 1.0:
                    if last_effective is not False:
                        title, _ = get_foreground_window_title()
                        logger.info(
                            "ACTIVE_SCREEN_CHANGED | allowed=False | title=%s",
                            title
                        )
                        hide_toast_if_needed()
                        last_effective = False

            time.sleep(0.20)

        except Exception as e:
            logger.exception(
                "ACTIVE_SCREEN_WATCHER_ERROR | %s",
                e
            )
            time.sleep(0.5)


# ============================================================
# SHUTDOWN
# ============================================================

def shutdown():

    if shutdown_event.is_set():
        return

    logger.info(
        "SHUTDOWN_REQUEST"
    )

    shutdown_event.set()

    try:
        scan_queue.put_nowait(None)
    except Exception:
        pass

# ============================================================
# MAIN
# ============================================================

def main():

    global app_tk_root
    global toast_mgr

    logger.info(
        "=================================================="
    )
    logger.info(
        "VIETTEL HIS MEDICATION CHECKER V82.6A5 FIX RESTART START"
    )
    logger.info(
        "Python=%s",
        sys.version.replace("\n", " ")
    )
    logger.info(
        "PID=%s",
        os.getpid()
    )
    logger.info(
        "=================================================="
    )

    app_tk_root = tk.Tk()
    app_tk_root.withdraw()

    toast_mgr = SafeToastManager(
        app_tk_root
    )

    # ----------------------------
    # UIA worker
    # ----------------------------

    start_uia_worker()

    # ----------------------------
    # Tray
    # ----------------------------

    if HAS_TRAY:
        threading.Thread(
            target=create_tray_icon,
            name="HIS-Tray",
            daemon=True
        ).start()
    else:
        logger.warning(
            "TRAY_DISABLED | pystray/Pillow not installed"
        )

    # ----------------------------
    # Auto update: mỗi ngày sau 01:00
    # ----------------------------

    threading.Thread(
        target=daily_update_loop,
        name="HIS-AutoUpdate",
        daemon=True
    ).start()

    # ----------------------------
    # Keyboard/mouse monitor
    # ----------------------------

    threading.Thread(
        target=monitor_actions,
        name="HIS-Monitor",
        daemon=True
    ).start()

    # ----------------------------
    # Active screen watcher - V82.2
    # ----------------------------
    threading.Thread(
        target=active_screen_watcher,
        name="HIS-ActiveScreen",
        daemon=True
    ).start()

    # ----------------------------
    # Watchdog
    # ----------------------------

    threading.Thread(
        target=watchdog_loop,
        name="HIS-Watchdog",
        daemon=True
    ).start()

    try:
        app_tk_root.mainloop()

    except KeyboardInterrupt:
        pass

    finally:
        shutdown()

        # Cho worker tối đa 2 giây kết thúc sạch.
        wt = worker_thread

        if wt and wt.is_alive():
            wt.join(timeout=2.0)

        logger.info(
            "VIETTEL HIS MEDICATION CHECKER V82.6A5 FIX RESTART EXIT"
        )

if __name__ == "__main__":
    main()
