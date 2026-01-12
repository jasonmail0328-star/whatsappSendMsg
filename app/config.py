# app/config.py
from pathlib import Path

BASE_DIR = Path.cwd()
ACCOUNTS_DIR = BASE_DIR / "accounts"
DATA_DIR = BASE_DIR / "data"
LOGS_DIR = BASE_DIR / "logs"
SCREENSHOTS_DIR = BASE_DIR / "screenshots"

DB_PATH = DATA_DIR / "bot.db"

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