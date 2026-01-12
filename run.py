#!/usr/bin/env python3
"""
run.py - 启动脚本（含自动引导 bootstrap）
- 在真正启动之前会调用 bootstrap.ensure_requirements()（通过 import bootstrap 并运行）
"""
import os
import sys

# 先运行本地 bootstrap（若存在），以确保依赖已安装
BOOTSTRAP_PY = os.path.join(os.path.dirname(__file__), "bootstrap.py")
if os.path.exists(BOOTSTRAP_PY):
    # 避免重复执行 bootstrap（bootstrap 自身会根据环境判断）
    try:
        # 执行 bootstrap as script to allow execv inside it
        with open(BOOTSTRAP_PY, "rb") as f:
            code = compile(f.read(), BOOTSTRAP_PY, "exec")
            exec(code, {"__name__": "__main__"})
    except SystemExit:
        # bootstrap may exit the process after instructions
        raise
    except Exception as e:
        print("Bootstrap 执行失败：", e)
        print("请检查 bootstrap.py 输出并手动安装依赖。")
        sys.exit(1)

# 现在应该可安全导入 app.server 并继续启动
import argparse
import threading
import time
import webbrowser
import shutil
import subprocess
import os
from app.server import create_app, run_uvicorn, ROOT_URL
from app.config import BROWSER_AUTO_OPEN_DELAY, BROWSER_LAUNCH_RETRIES
from app.logging_config import logger

def _launch_browser_executable(exe_path: str, url: str) -> bool:
    if not exe_path:
        return False
    try:
        exe = exe_path
        if not os.path.isabs(exe_path):
            found = shutil.which(exe_path)
            if found:
                exe = found
        args = [exe, "--new-window", url]
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
        return True
    except Exception as e:
        logger.warning("Browser launch failed for %s: %s", exe_path, e)
        return False

def _open_browser_later(url: str, delay: float = 1.0, preferred_browser: str = None, retries: int = 1):
    def _target():
        time.sleep(delay)
        tried = False
        if preferred_browser:
            tried = _launch_browser_executable(preferred_browser, url)
        if not tried:
            for name in ("chrome", "chrome.exe", "msedge", "msedge.exe", "firefox", "firefox.exe"):
                if shutil.which(name):
                    ok = _launch_browser_executable(name, url)
                    if ok:
                        tried = True
                        break
        if not tried:
            try:
                webbrowser.open_new(url)
            except Exception as e:
                logger.error("自动打开浏览器失败：%s 。请手动打开：%s", e, url)
    t = threading.Thread(target=_target, daemon=True)
    t.start()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-open", action="store_true", help="启动服务但不要自动打开浏览器")
    parser.add_argument("--browser", type=str, default=None, help="首选浏览器（chrome/msedge/firefox 或可执行路径）")
    parser.add_argument("--delay", type=float, default=BROWSER_AUTO_OPEN_DELAY, help="自动打开浏览器延迟（秒）")
    args = parser.parse_args()

    app = create_app()

    logger.info("Starting WhatsApp Manager server, opening UI at %s", ROOT_URL)
    if not args.no_open:
        _open_browser_later(ROOT_URL, delay=args.delay, preferred_browser=args.browser, retries=BROWSER_LAUNCH_RETRIES)

    run_uvicorn(app)

if __name__ == "__main__":
    main()