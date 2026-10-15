# -*- coding: utf-8 -*-
# A6 TEST LOG FOLDER - updater download goes to ./logs
# AUTOUPDATE TEST A5 - harmless version bump for updater validation
"""
Viettel HIS Medication Checker - V82.6A16 NO ADMIN TEST AUTOUPDATE GITHUB / MAX SPEED
--------------------------------------------------------------
Mục tiêu:
- Tự động giữ nguyên Shortcut ngoài Desktop khi Auto-Update.
- Fix lỗi update bị chặn bởi Single Instance (Mutex).
- Giữ nguyên tính năng tự động update lúc 1h sáng.
- Sửa lỗi bắt nhầm "1g" (1 gam) thành 1 giờ (01:00) trong Cách dùng.
- Sửa lỗi không đọc được DVT trong ngoặc vuông do có dấu cách (VD: "[ 1 viên /ngày]").
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

APP_VERSION = "82.6A16"
APP_VERSION_CODE = 82616

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
    "logs",
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
# SINGLE INSTANCE CHECK (CHỐNG MỞ NHIỀU APP)
# ============================================================
_app_mutex = None

def enforce_single_instance():
    global _app_mutex
    # Bỏ qua check mutex nếu đang restart từ auto-updater
    if len(sys.argv) > 1 and "update_restart" in sys.argv:
        logger.info("APP_START | Bypassing Mutex check during auto-update restart.")
        return

    ERROR_ALREADY_EXISTS = 183
    mutex_name = "ViettelHISChecker_SingleInstance_Mutex_V82"
    _app_mutex = kernel32.CreateMutexW(None, False, mutex_name)
    
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        logger.warning("APP_ALREADY_RUNNING | Exiting new instance.")
        user32.MessageBoxW(
            0, 
            "Phần mềm Viettel HIS Checker ĐANG ĐƯỢC CHẠY NGẦM rồi!\n\n"
            "Vui lòng kiểm tra biểu tượng ứng dụng ở góc dưới cùng bên phải màn hình (chỗ xem giờ). "
            "Bạn không cần mở thêm nữa.", 
            "Thông báo", 
            0x40
        )
        sys.exit(0)

def release_single_instance():
    """Giải phóng Mutex để bản mới có thể chạy lên được sau khi bản này thoát."""
    global _app_mutex
    if _app_mutex:
        try:
            kernel32.ReleaseMutex(_app_mutex)
            kernel32.CloseHandle(_app_mutex)
            _app_mutex = None
            logger.info("MUTEX_RELEASED")
        except Exception as e:
            logger.error("MUTEX_RELEASE_ERROR | %s", e)


# ============================================================
# DESKTOP SHORTCUT HELPER (GIỮ VÀ TỰ PHỤC HỒI SHORTCUT)
# ============================================================

def ensure_desktop_shortcut():
    """Đảm bảo Shortcut ngoài Desktop luôn trỏ vào đúng file đang chạy."""
    try:
        desktop = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
        shortcut_path = os.path.join(desktop, "Viettel HIS Checker.lnk")
        current_script = os.path.abspath(sys.argv[0])

        if not os.path.exists(shortcut_path):
            pyexe = sys.executable
            pyw = os.path.join(os.path.dirname(pyexe), "pythonw.exe")
            target_exe = pyw if os.path.exists(pyw) else pyexe

            vbs = (
                f'Set ws = CreateObject("WScript.Shell")\n'
                f'Set sc = ws.CreateShortcut("{shortcut_path}")\n'
                f'sc.TargetPath = "{target_exe}"\n'
                f'sc.Arguments = "\"{current_script}\""\n'
                f'sc.WorkingDirectory = "{os.path.dirname(current_script)}"\n'
                f'sc.Description = "Viettel HIS Medication Checker"\n'
                f'sc.Save\n'
            )
            vbs_file = os.path.join(os.environ.get("TEMP", ""), "create_his_sc.vbs")
            with open(vbs_file, "w", encoding="utf-8") as f:
                f.write(vbs)
            os.system(f'cscript //nologo "{vbs_file}"')
            try:
                os.remove(vbs_file)
            except Exception:
                pass
            logger.info("DESKTOP_SHORTCUT_RESTORED")
    except Exception as e:
        logger.warning("ENSURE_SHORTCUT_FAILED | %s", e)


# ============================================================
# CLEAN LEGACY UPDATE FILES
# ============================================================

def _cleanup_legacy_update_files():
    try:
        backup_dir = os.path.join(LOG_DIR, "backup")
        os.makedirs(backup_dir, exist_ok=True)

        legacy_state = os.path.join(APP_DIR, ".update_state.json")
        if os.path.isfile(legacy_state):
            target = os.path.join(LOG_DIR, ".update_state.json")
            try:
                if not os.path.exists(target):
                    os.replace(legacy_state, target)
                else:
                    os.remove(legacy_state)
            except Exception:
                pass

        legacy_bak = os.path.abspath(sys.argv[0]) + ".bak"
        if os.path.isfile(legacy_bak):
            target = os.path.join(
                backup_dir,
                "Viettel_HIS_Medication_Checker.previous.py"
            )
            try:
                os.replace(legacy_bak, target)
            except Exception:
                pass

        legacy_update = os.path.abspath(sys.argv[0]) + ".update_new.py"
        if os.path.isfile(legacy_update):
            try:
                os.remove(legacy_update)
            except Exception:
                pass

    except Exception as e:
        logger.warning("UPDATE_CLEANUP_FAILED | %s", e)


_cleanup_legacy_update_files()

# ============================================================
# NO ADMIN TEST
# ============================================================

logger.info("NO_ADMIN_MODE | elevation_request=disabled")

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
        os.makedirs(os.path.dirname(UPDATE_STATE_FILE), exist_ok=True)
        with open(UPDATE_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_update_state(data):
    try:
        os.makedirs(os.path.dirname(UPDATE_STATE_FILE), exist_ok=True)
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


def _write_self_replace_helper(new_file, current_file):
    pid = os.getpid()
    app_dir = os.path.dirname(current_file)

    backup_dir = os.path.join(app_dir, "logs", "backup")
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(
        backup_dir,
        "Viettel_HIS_Medication_Checker.previous.py"
    )

    restart_log = os.path.join(app_dir, "logs", "update_restart.log")

    pyexe = sys.executable
    pyw_candidate = os.path.join(os.path.dirname(pyexe), "pythonw.exe")
    restart_exe = pyw_candidate if os.path.isfile(pyw_candidate) else pyexe

    temp_dir = os.environ.get("TEMP") or app_dir
    helper_file = os.path.join(temp_dir, f"viettel_his_update_{pid}.py")

    # Dùng shutil.copy2 ghi đè nội dung file gốc để GIỮ NGUYÊN FILE ID -> KHÔNG BỊ HỎNG SHORTCUT
    helper_code = '# -*- coding: utf-8 -*-\nimport os\nimport sys\nimport time\nimport shutil\nimport ctypes\nimport subprocess\nimport traceback\n\ncurrent_file = sys.argv[1]\nnew_file = sys.argv[2]\nbackup = sys.argv[3]\nrestart_exe = sys.argv[4]\nold_pid = int(sys.argv[5])\nlog_file = sys.argv[6]\n\ndef log(msg):\n    try:\n        os.makedirs(os.path.dirname(log_file), exist_ok=True)\n        with open(log_file, "a", encoding="utf-8") as f:\n            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + " | " + msg + "\\n")\n    except Exception:\n        pass\n\ntry:\n    log("HELPER_START | pid=%s" % old_pid)\n\n    SYNCHRONIZE = 0x00100000\n    kernel32 = ctypes.windll.kernel32\n    h = kernel32.OpenProcess(SYNCHRONIZE, False, old_pid)\n\n    if h:\n        rc = kernel32.WaitForSingleObject(h, 30000)\n        kernel32.CloseHandle(h)\n        log("WAIT_OLD_DONE | rc=%s" % rc)\n    else:\n        log("WAIT_OLD_OPENPROCESS_FAILED_ASSUME_EXITED")\n\n    time.sleep(1.0)\n\n    replaced = False\n    last_error = ""\n\n    for attempt in range(1, 21):\n        try:\n            if os.path.exists(current_file):\n                try:\n                    os.makedirs(os.path.dirname(backup), exist_ok=True)\n                    shutil.copy2(current_file, backup)\n                except Exception as e:\n                    log("BACKUP_WARN | attempt=%s | %r" % (attempt, e))\n\n            # Ghi đè trực tiếp để giữ nguyên File ID và Shortcut liên kết\n            shutil.copy2(new_file, current_file)\n            try:\n                os.remove(new_file)\n            except Exception:\n                pass\n            replaced = True\n            log("REPLACE_OK | attempt=%s" % attempt)\n            break\n\n        except Exception as e:\n            last_error = repr(e)\n            log("REPLACE_RETRY | attempt=%s | %s" % (attempt, last_error))\n            time.sleep(0.5)\n\n    if not replaced:\n        raise RuntimeError("Không thay được file sau 20 lần: " + last_error)\n\n    if not os.path.isfile(current_file):\n        raise RuntimeError("File chính không tồn tại sau replace")\n\n    time.sleep(0.5)\n\n    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)\n    # Thêm tham số update_restart để bản mới bỏ qua check Mutex\n    p = subprocess.Popen(\n        [restart_exe, current_file, "update_restart"],\n        cwd=os.path.dirname(current_file),\n        creationflags=flags,\n        close_fds=True\n    )\n\n    log("RESTART_OK | exe=%s | new_pid=%s" % (restart_exe, p.pid))\n\nexcept Exception:\n    log("HELPER_FATAL | " + traceback.format_exc().replace("\\n", " || "))\n\nfinally:\n    try:\n        os.remove(__file__)\n    except Exception:\n        pass\n'

    with open(helper_file, "w", encoding="utf-8") as f:
        f.write(helper_code)

    return helper_file, backup, restart_exe, restart_log


def _install_downloaded_update(new_file, version):
    current_file = os.path.abspath(sys.argv[0])

    if not current_file.lower().endswith(".py"):
        raise RuntimeError("Bản auto-update hiện tại hỗ trợ file .py")

    helper_file, backup, restart_exe, restart_log = (
        _write_self_replace_helper(new_file, current_file)
    )

    logger.info(
        "UPDATE_INSTALL_READY | current=%s | new=%s | helper=%s | restart_exe=%s | restart_log=%s | version=%s",
        current_file,
        new_file,
        helper_file,
        restart_exe,
        restart_log,
        version
    )
    
    release_single_instance()

    subprocess.Popen(
        [
            sys.executable,
            helper_file,
            current_file,
            new_file,
            backup,
            restart_exe,
            str(os.getpid()),
            restart_log
        ],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True
    )

    shutdown_event.set()

    try:
        if app_tk_root:
            app_tk_root.after(0, app_tk_root.quit)
    except Exception:
        pass


def _get_update_download_dir():
    app_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    logs_dir = os.path.join(app_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    return logs_dir


def check_for_update(manual=False):
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

        os.makedirs(LOG_DIR, exist_ok=True)
        manifest_copy = os.path.join(LOG_DIR, "version.json")
        with open(manifest_copy, "wb") as f:
            f.write(raw)

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

        if len(new_bytes) < 5000 or b"Viettel HIS Medication Checker" not in new_bytes[:12000]:
            raise RuntimeError("File tải về không giống checker hợp lệ")

        actual_sha = hashlib.sha256(new_bytes).hexdigest()
        if expected_sha and actual_sha != expected_sha:
            raise RuntimeError(
                "SHA256 không khớp: file tải về có thể lỗi hoặc chưa đồng bộ"
            )

        current_file = os.path.abspath(sys.argv[0])
        new_file = os.path.join(_get_update_download_dir(), "Viettel_HIS_Medication_Checker.update.py")
        logger.info("UPDATE_DOWNLOAD_PATH | %s", new_file)

        with open(new_file, "wb") as f:
            f.write(new_bytes)

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

        archive_file = os.path.join(
            LOG_DIR,
            f"Viettel_HIS_Medication_Checker_{version}.py"
        )
        with open(archive_file, "wb") as f:
            f.write(new_bytes)

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
    while not shutdown_event.is_set():
        try:
            now = datetime.datetime.now()
            today = now.date().isoformat()
            state = _load_update_state()
            last_day = state.get("last_auto_check_day", "")

            if now.hour >= UPDATE_CHECK_HOUR and last_day != today:
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

    # FIX: Cho phép khoảng trắng sau ngoặc vuông/tròn và trước số (VD: "[ 1 viên /ngày]")
    m_exp = re.search(
        r"[\[\(]\s*(\d+(?:[.,]\d+)?)\s*([a-zA-ZÀ-ỹ]+)?\s*/\s*"
        r"(?:ngày|ngay)\s*[\]\)]",
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

    # FIX: Tránh nhầm "1g" (1 gam) thành 1 giờ
    for m in re.finditer(
        r"\b(\d{1,2})\s*(?:h|:|giờ)\s*(\d{1,2})?\b",
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

    for word in ("sáng", "trưa", "chiều", "tối", "đêm"):
        if re.search(rf"\b{word}\b", text, flags=re.I) and word not in time_points:
            time_points.append(word)

    return len(time_points), time_points


def check_row(row):
    dvt_raw = row.get("unit", "")

    if not is_medication_by_unit(dvt_raw):
        return {
            "status": "OK",
            "row": row,
            "errors": []
        }

    dose_raw = norm_text(row.get("dose", ""))
    usage_raw = norm_text(row.get("usage", ""))

    dose = parse_dose_robust(dose_raw)
    dvt = normalize_unit(dvt_raw)
    qty = parse_number(row.get("qty", ""))
    errors = []

    # 1. KIỂM TRA LIỀU DÙNG
    if not dose_raw or dose["calc_total"] is None:
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

    # 2. KIỂM TRA CÁCH DÙNG
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
    "XUẤT TỦ TRỰC",
    "XUAT TU TRUC",
)

HARD_BLOCK_SCREEN_KEYWORDS = (
    "XUẤT GÓI",
    "XUAT GOI",
    "XUẤT GÓI THUỐC",
    "XUẤT GÓI DỊCH VỤ",
    "XUẤT GÓI ĐIỀU CHỈNH",
    "XUẤT TỪ TRỰC",
    "XUAT TU TRUC",
)

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
    title, hwnd = get_foreground_window_title()
    if not hwnd or not title:
        return False

    upper = norm_text(title).upper()

    return any(
        screen == upper or screen in upper
        for screen in ALLOWED_MEDICATION_SCREENS
    )


def is_hard_block_screen_active():
    title, hwnd = get_foreground_window_title()
    if not hwnd or not title:
        return False

    upper = title.upper()
    return any(k in upper for k in HARD_BLOCK_SCREEN_KEYWORDS)


def is_allowed_medication_screen_active(use_grace=True):
    global _last_allowed_screen_time, _last_allowed_screen_title

    title, hwnd = get_foreground_window_title()
    now = time.monotonic()

    if hwnd and title:
        upper = norm_text(title).upper()

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
    "HỘI CHẨN",
    "XUẤT TỦ TRỰC",
    "XUAT TU TRUC"
)

def find_his_hwnd_win32():
    try:
        fg = user32.GetForegroundWindow()

        if fg:
            root = user32.GetAncestor(fg, 2) or fg

            length = user32.GetWindowTextLengthW(root)

            if length > 0:
                buf = ctypes.create_unicode_buffer(
                    max(length + 1, 512)
                )
                user32.GetWindowTextW(
                    root,
                    buf,
                    len(buf)
                )

                title = norm_text(
                    buf.value
                ).upper()

                if any(
                    screen in title
                    for screen in ALLOWED_MEDICATION_SCREENS
                ):
                    return root

    except Exception:
        pass

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

            buf = ctypes.create_unicode_buffer(
                length + 1
            )
            user32.GetWindowTextW(
                h,
                buf,
                length + 1
            )

            title = buf.value.upper()

            if any(
                k in title
                for k in HIS_TITLE_KEYWORDS
            ):
                found = h
                return False

        except Exception:
            pass

        return True

    try:
        user32.EnumWindows(
            EnumProc(enum_cb),
            0
        )
    except Exception:
        pass

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
# EXACT NAMED CELL PARSER - V82
# ============================================================

CELL_ROW_RE = re.compile(r"\bRow\s+(\d+)\b", flags=re.I)


def _field_from_uia_cell_name(name):
    low = norm_text(name).lower()

    if "row " not in low:
        return None, None

    m = CELL_ROW_RE.search(name)
    if not m:
        return None, None

    row_no = int(m.group(1))

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
    try:
        v = el.GetCurrentPropertyValue(30045)
        if v is not None:
            s = norm_text(str(v))
            if s and s.lower() not in {"null", "none"}:
                return s
    except Exception:
        pass

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
            return True

        except Exception:
            self.uia = None
            self.initialized = False
            return False

    def reset(self, reason="unknown"):
        old = self.uia
        self.uia = None
        self.initialized = False

        try:
            del old
        except Exception:
            pass

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
        if not self.initialize():
            return []

        uia = self.uia
        hwnd = find_his_hwnd_win32()
        if not hwnd:
            return []

        win_elem = None
        header = None
        target_grid = None
        elements = None

        try:
            win_elem = uia.ElementFromHandle(hwnd)
            target_grid = win_elem

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

            elements = target_grid.FindAll(4, uia.CreateTrueCondition())
            rows = {}

            for i in range(elements.Length):
                try:
                    el = elements.GetElement(i)

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

                if not is_medication_by_unit(r.get("unit", "")):
                    continue

                r["_row_no"] = row_no
                all_rows.append(r)

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
                self.window.after_cancel(self.after_id)
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

        outer = tk.Frame(self.window, bg="#d32f2f", padx=2, pady=2)
        outer.pack(fill="both", expand=True)

        inner = tk.Frame(outer, bg="#fffde7", padx=8, pady=6)
        inner.pack(fill="both", expand=True)

        hf = tk.Frame(inner, bg="#fffde7")
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
        btn.bind("<Button-1>", lambda e: self.hide_toast())

        for item in error_items:
            med_name = item["row"].get("name", "")
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

        self.after_id = self.window.after(POPUP_DURATION * 1000, self.hide_toast)

    def hide_toast(self):
        if self.after_id and self.window:
            try:
                self.window.after_cancel(self.after_id)
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
    t0 = time.perf_counter()
    set_worker_state(status="SCANNING", last_start=time.monotonic(), last_error="")

    if app_tk_root:
        try:
            app_tk_root.after(0, toast_mgr.hide_toast)
        except Exception:
            pass

    try:
        all_rows = uia_core.extract_anti_ghost()
        results = [check_row(r) for r in all_rows]
        error_items = [x for x in results if x["status"] == "ERROR"]

        duration_ms = (time.perf_counter() - t0) * 1000.0
        set_worker_state(
            status="HEALTHY",
            last_finish=time.monotonic(),
            last_duration_ms=duration_ms,
            scan_count=get_worker_state()["scan_count"] + 1
        )

        if app_tk_root:
            try:
                app_tk_root.after(0, lambda errs=error_items: toast_mgr.update_toast(errs))
            except Exception:
                pass

        return True

    except Exception as e:
        duration_ms = (time.perf_counter() - t0) * 1000.0
        set_worker_state(
            status="RECOVERING",
            last_finish=time.monotonic(),
            last_duration_ms=duration_ms,
            last_error=f"{type(e).__name__}: {e}"
        )
        return False

# ============================================================
# UI AUTOMATION WORKER
# ============================================================

class UIAutomationWorker(threading.Thread):

    def __init__(self):
        super().__init__(name="HIS-UIA-Worker", daemon=True)
        self.uia_core = None
        self.com_initialized = False

    def initialize_com(self):
        try:
            if hasattr(comtypes, "CoInitializeEx"):
                comtypes.CoInitializeEx(0)
            else:
                comtypes.CoInitialize()
            self.com_initialized = True
            return True
        except Exception:
            self.com_initialized = False
            return False

    def uninitialize_com(self):
        if not self.com_initialized:
            return
        try:
            comtypes.CoUninitialize()
        except Exception:
            pass
        self.com_initialized = False

    def recover_uia(self, reason):
        set_worker_state(status="RECOVERING", last_error=str(reason))
        try:
            if self.uia_core:
                self.uia_core.reset(reason=str(reason))
            else:
                self.uia_core = UIAutomationCore()
                self.uia_core.initialize()
        except Exception:
            pass

    def run(self):
        if not self.initialize_com():
            set_worker_state(status="ERROR", last_error="COM initialization failed")
            worker_ready.set()
            return

        try:
            self.uia_core = UIAutomationCore()
            if not self.uia_core.initialize():
                set_worker_state(status="ERROR", last_error="UIAutomation initialization failed")

            worker_ready.set()
            set_worker_state(status="HEALTHY")

            while not shutdown_event.is_set():
                try:
                    source = scan_queue.get(timeout=0.25)
                except queue.Empty:
                    continue

                if source is None:
                    break

                ok = run_scan_once(self.uia_core, source)
                if not ok:
                    self.recover_uia(f"scan failed: {source}")
                    run_scan_once(self.uia_core, source + " [RETRY]")

                scan_queue.task_done()

        except Exception as e:
            set_worker_state(status="ERROR", last_error=f"{type(e).__name__}: {e}")
        finally:
            if self.uia_core:
                self.uia_core.close()
            self.uia_core = None
            self.uninitialize_com()
            set_worker_state(status="STOPPED")

def start_uia_worker():
    global worker_thread
    worker_ready.clear()
    worker_thread = UIAutomationWorker()
    worker_thread.start()
    worker_ready.wait(timeout=3.0)

# ============================================================
# TRIGGER
# ============================================================

def trigger_scan(source, _transient_retry=0):
    if shutdown_event.is_set():
        return

    current_title, _ = get_foreground_window_title()
    current_upper = norm_text(current_title).upper()

    is_transient = (not current_upper or current_upper in ("THÔNG BÁO", "THONG BAO"))
    if is_transient:
        if _transient_retry < 3:
            timer = threading.Timer(0.25, lambda: trigger_scan(source, _transient_retry + 1))
            timer.daemon = True
            timer.start()
            return
        hide_toast_if_needed()
        return

    if is_hard_block_screen_active() or not is_allowed_medication_screen_active(use_grace=True):
        hide_toast_if_needed()
        return

    try:
        scan_queue.put_nowait(source)
    except queue.Full:
        pass

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
                if not shutdown_event.is_set():
                    start_uia_worker()
                continue

            state = get_worker_state()
            if state["status"] == "SCANNING":
                elapsed = time.monotonic() - state["last_start"]
                if elapsed > WATCHDOG_STALL_SECONDS:
                    set_worker_state(
                        status="RECOVERING",
                        last_error=f"scan exceeded {WATCHDOG_STALL_SECONDS}s"
                    )
        except Exception:
            pass

# ============================================================
# TRAY
# ============================================================

def create_tray_icon():
    if not HAS_TRAY:
        return

    try:
        image = Image.new("RGBA", (64, 64), color=(255, 255, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((0, 0, 64, 64), fill=(41, 128, 185))
        draw.rectangle((26, 12, 38, 52), fill="white")
        draw.rectangle((12, 26, 52, 38), fill="white")

        def on_quit(icon, item):
            shutdown()
            try:
                icon.stop()
            except Exception:
                pass

        def on_update(icon, item):
            update_now_async()

        def on_version(icon, item):
            _show_info("Viettel HIS Checker", f"Phiên bản hiện tại: {APP_VERSION}")

        def on_open_log(icon, item):
            try:
                os.startfile(LOG_FILE)
            except Exception:
                pass

        menu = pystray.Menu(
            pystray.MenuItem("Kiểm tra cập nhật ngay", on_update),
            pystray.MenuItem(f"Phiên bản: {APP_VERSION}", on_version),
            pystray.MenuItem("Mở log", on_open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Thoát phần mềm (Exit)", on_quit)
        )

        icon = pystray.Icon("HIS_Checker", image, "Viettel HIS Checker", menu)
        icon.run()

    except Exception:
        pass

# ============================================================
# MOUSE / KEYBOARD MONITOR
# ============================================================

CLICK_KEYWORDS = ("chấp nhận", "chap nhan", "đồng ý", "dong y", "lưu", "luu", "f2", "ok")
CLICK_IGNORE = ("cách dùng", "liều dùng", "xoá", "xóa", "xuất gói", "xuat goi", "xuất từ trực", "xuat tu truc")

def check_click_and_trigger():
    try:
        if is_hard_block_screen_active():
            hide_toast_if_needed()
            return

        pt = POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return

        hwnd = user32.WindowFromPoint(pt)
        if not hwnd:
            return

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return

        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        name = buf.value.lower()

        if any(k in name for k in CLICK_KEYWORDS) and not any(ign in name for ign in CLICK_IGNORE):
            time.sleep(0.06)
            trigger_scan("CLICK XÁC NHẬN")

    except Exception:
        pass

def monitor_actions():
    f2_down = False
    f4_down = False
    lb_down = False
    last_trig = 0.0

    while not shutdown_event.is_set():
        try:
            now = time.monotonic()
            is_f2 = bool(user32.GetAsyncKeyState(VK_F2) & 0x8000)
            if is_f2 and not f2_down and now - last_trig > 0.5:
                trigger_scan("F2 (LƯU)")
                last_trig = now
            f2_down = is_f2

            is_f4 = bool(user32.GetAsyncKeyState(VK_F4) & 0x8000)
            if is_f4 and not f4_down and now - last_trig > 0.5:
                trigger_scan("F4 (SỬA)")
                last_trig = now
            f4_down = is_f4

            is_lb = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
            if is_lb and not lb_down and now - last_trig > 0.5:
                threading.Thread(target=check_click_and_trigger, daemon=True).start()
                last_trig = now
            lb_down = is_lb

            time.sleep(0.015)
        except Exception:
            time.sleep(0.1)

# ============================================================
# ACTIVE SCREEN WATCHER - V82.2
# ============================================================

def active_screen_watcher():
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

            if hard_block:
                was_in_hard_block = True
                return_rescan_done = False
                hide_toast_if_needed()

            elif was_in_hard_block and exact_allowed and not return_rescan_done:
                time.sleep(0.25)
                if is_exact_allowed_medication_screen_active():
                    trigger_scan("AUTO RETURN SAU XUẤT GÓI")
                    return_rescan_done = True
                    was_in_hard_block = False

            if allowed:
                wrong_since = None
                if last_effective is not True:
                    last_effective = True
            else:
                if wrong_since is None:
                    wrong_since = now
                if now - wrong_since >= 1.0:
                    if last_effective is not False:
                        hide_toast_if_needed()
                        last_effective = False

            time.sleep(0.20)
        except Exception:
            time.sleep(0.5)

# ============================================================
# SHUTDOWN
# ============================================================

def shutdown():
    release_single_instance()
    if shutdown_event.is_set():
        return

    logger.info("SHUTDOWN_REQUEST")
    shutdown_event.set()

    try:
        scan_queue.put_nowait(None)
    except Exception:
        pass

# ============================================================
# MAIN
# ============================================================

def main():
    enforce_single_instance()
    ensure_desktop_shortcut()

    global app_tk_root
    global toast_mgr

    logger.info("VIETTEL HIS MEDICATION CHECKER V82.6A16 NO ADMIN TEST START")

    app_tk_root = tk.Tk()
    app_tk_root.withdraw()
    toast_mgr = SafeToastManager(app_tk_root)

    start_uia_worker()

    if HAS_TRAY:
        threading.Thread(target=create_tray_icon, name="HIS-Tray", daemon=True).start()

    threading.Thread(target=daily_update_loop, name="HIS-AutoUpdate", daemon=True).start()
    threading.Thread(target=monitor_actions, name="HIS-Monitor", daemon=True).start()
    threading.Thread(target=active_screen_watcher, name="HIS-ActiveScreen", daemon=True).start()
    threading.Thread(target=watchdog_loop, name="HIS-Watchdog", daemon=True).start()

    try:
        app_tk_root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
        wt = worker_thread
        if wt and wt.is_alive():
            wt.join(timeout=2.0)
        logger.info("VIETTEL HIS MEDICATION CHECKER V82.6A16 NO ADMIN TEST EXIT")

if __name__ == "__main__":
    main()