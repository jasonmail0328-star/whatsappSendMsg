# app/config.py
from pathlib import Path
import json

BASE_DIR = Path.cwd()
ACCOUNTS_DIR = BASE_DIR / "accounts"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"

DB_PATH = DATA_DIR / "bot.db"
SETTINGS_FILE = DATA_DIR / "settings.json"

HOST = "127.0.0.1"
PORT = 8000
ROOT_URL = f"http://{HOST}:{PORT}"

# Playwright login wait time (seconds)
LOGIN_TIMEOUT = 180
# QR detect retry times (for detecting phone/jid after login)
QR_DETECT_RETRIES = 4

# Default timeouts
PAGE_TIMEOUT = 60  # seconds

# 并发控制：同时允许的最大发送任务数（可根据机器调整）
MAX_CONCURRENT_SENDS = 2

# 批量发送轮询间隔（秒，前端轮询使用）
BULK_POLL_INTERVAL = 1.5

# 日志配置
LOG_DIR = LOGS_DIR
LOG_FILE = LOG_DIR / "whatsapp_manager.log"
LOG_LEVEL = "INFO"  # 可改为 "DEBUG"

# run.py 可用的浏览器启动默认行为
BROWSER_AUTO_OPEN_DELAY = 1.0  # seconds
BROWSER_LAUNCH_RETRIES = 2

# 默认 dry_run
DEFAULT_DRY_RUN = True

# --------- 新增：发送相关可配置项（UI 可修改并持久化） ----------
# 每次切换账号之间的默认间隔（秒）——UI 可覆盖
DEFAULT_ACCOUNT_INTERVAL = 1.0

# 每轮账号列表轮询完成后的默认间隔（秒），用于多条信息时在开始下一轮前等待
DEFAULT_ROUND_INTERVAL = 5.0

# 每个字符敲入的随机延迟范围（秒），用于放慢“打字”速度
# 实际每个字符等待 random.uniform(CHAR_DELAY_MIN, CHAR_DELAY_MAX)
CHAR_DELAY_MIN = 0.05
CHAR_DELAY_MAX = 0.18

# 并发控制（UI 可编辑）
# MAX_CONCURRENT_SENDS 已声明上面，作为默认值

# 其他可保存项：BULK_POLL_INTERVAL 可由 UI 控制前端轮询间隔（但前端已直接在 UI JS 使用固定 1500ms）
# 为简便，保留 BULK_POLL_INTERVAL 为可保存项

# ----------------- 设置持久化与加载函数 -----------------
def _ensure_data_dir():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

def load_settings():
    """
    Load settings from SETTINGS_FILE (data/settings.json) and override module variables.
    """
    _ensure_data_dir()
    try:
        if SETTINGS_FILE.exists():
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                j = json.load(fh)
            # allowed keys and expected types, set if present
            if "MAX_CONCURRENT_SENDS" in j:
                try:
                    global MAX_CONCURRENT_SENDS
                    MAX_CONCURRENT_SENDS = int(j["MAX_CONCURRENT_SENDS"])
                except Exception:
                    pass
            if "BULK_POLL_INTERVAL" in j:
                try:
                    global BULK_POLL_INTERVAL
                    BULK_POLL_INTERVAL = float(j["BULK_POLL_INTERVAL"])
                except Exception:
                    pass
            if "DEFAULT_ACCOUNT_INTERVAL" in j:
                try:
                    global DEFAULT_ACCOUNT_INTERVAL
                    DEFAULT_ACCOUNT_INTERVAL = float(j["DEFAULT_ACCOUNT_INTERVAL"])
                except Exception:
                    pass
            if "DEFAULT_ROUND_INTERVAL" in j:
                try:
                    global DEFAULT_ROUND_INTERVAL
                    DEFAULT_ROUND_INTERVAL = float(j["DEFAULT_ROUND_INTERVAL"])
                except Exception:
                    pass
            if "CHAR_DELAY_MIN" in j:
                try:
                    global CHAR_DELAY_MIN
                    CHAR_DELAY_MIN = float(j["CHAR_DELAY_MIN"])
                except Exception:
                    pass
            if "CHAR_DELAY_MAX" in j:
                try:
                    global CHAR_DELAY_MAX
                    CHAR_DELAY_MAX = float(j["CHAR_DELAY_MAX"])
                except Exception:
                    pass
    except Exception:
        # ignore and keep defaults
        pass

def save_settings(settings: dict):
    """
    Save provided settings dict to SETTINGS_FILE and update module variables.
    Accept keys: MAX_CONCURRENT_SENDS, BULK_POLL_INTERVAL, DEFAULT_ACCOUNT_INTERVAL,
                 DEFAULT_ROUND_INTERVAL, CHAR_DELAY_MIN, CHAR_DELAY_MAX
    Returns the dict actually saved (normalized).
    """
    _ensure_data_dir()
    # Load existing
    cur = {}
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
                cur = json.load(fh)
        except Exception:
            cur = {}

    # Update values validated
    out = dict(cur)
    def set_int(key, val):
        try:
            out[key] = int(val)
        except Exception:
            pass
    def set_float(key, val):
        try:
            out[key] = float(val)
        except Exception:
            pass

    if "MAX_CONCURRENT_SENDS" in settings:
        set_int("MAX_CONCURRENT_SENDS", settings["MAX_CONCURRENT_SENDS"])
    if "BULK_POLL_INTERVAL" in settings:
        set_float("BULK_POLL_INTERVAL", settings["BULK_POLL_INTERVAL"])
    if "DEFAULT_ACCOUNT_INTERVAL" in settings:
        set_float("DEFAULT_ACCOUNT_INTERVAL", settings["DEFAULT_ACCOUNT_INTERVAL"])
    if "DEFAULT_ROUND_INTERVAL" in settings:
        set_float("DEFAULT_ROUND_INTERVAL", settings["DEFAULT_ROUND_INTERVAL"])
    if "CHAR_DELAY_MIN" in settings:
        set_float("CHAR_DELAY_MIN", settings["CHAR_DELAY_MIN"])
    if "CHAR_DELAY_MAX" in settings:
        set_float("CHAR_DELAY_MAX", settings["CHAR_DELAY_MAX"])

    # Write back
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, ensure_ascii=False)
    except Exception:
        pass

    # Apply to module variables (best-effort)
    try:
        load_settings()
    except Exception:
        pass

    return out

# Load settings on import to override defaults if saved
load_settings()