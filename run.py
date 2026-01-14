# run.py
# 启动服务器并在浏览器中自动打开 UI 页面（等待服务ready后再打开）
import time
import threading
import webbrowser
import sys
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

# 尝试从 app.server 导入 create_app, run_uvicorn, HOST, PORT
try:
    from app.server import create_app, run_uvicorn, HOST, PORT  # type: ignore
except Exception:
    # 兼容性回退：如果 app.server 没有 run_uvicorn，导入 create_app 并使用 uvicorn.run
    try:
        from app.server import create_app, HOST, PORT  # type: ignore
        run_uvicorn = None
    except Exception as e:
        print("无法从 app.server 导入 create_app:", e)
        raise

APP_HOST = HOST if 'HOST' in globals() else "127.0.0.1"
APP_PORT = int(PORT) if 'PORT' in globals() else 8000
BASE_URL = f"http://{APP_HOST}:{APP_PORT}"

def open_browser_when_ready(url: str, timeout: float = 10.0, check_interval: float = 0.25):
    """
    Poll the server until it's responding (or timeout), then open the default browser.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            # Try a lightweight GET; if server responds, open browser
            with urlopen(url, timeout=1) as resp:
                # If we get any HTTP response, proceed to open browser
                break
        except (URLError, HTTPError, OSError):
            time.sleep(check_interval)
            continue
    try:
        webbrowser.open(url)
    except Exception as e:
        print("无法自动打开浏览器，请手动打开：", url, "（错误：", e, ")")

def start_server_and_browser():
    app = create_app()

    # Start server
    server_thread = None
    if 'run_uvicorn' in globals() and callable(run_uvicorn):
        try:
            # run_uvicorn(app, host=APP_HOST, port=APP_PORT, reload=False, block=False)
            server_thread = run_uvicorn(app=app, host=APP_HOST, port=APP_PORT, reload=False, block=False)
            print(f"Starting server in background thread at {BASE_URL}")
        except Exception as e:
            print("run_uvicorn 调用失败，尝试退回到 uvicorn.run:", e)
            server_thread = None

    if server_thread is None:
        # fallback: start uvicorn in a thread
        try:
            import uvicorn
            def _run_uvicorn():
                uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="info")
            server_thread = threading.Thread(target=_run_uvicorn, daemon=True)
            server_thread.start()
            print(f"Starting uvicorn in background thread at {BASE_URL}")
        except Exception as e:
            print("无法启动 uvicorn:", e)
            raise

    # Start browser opener in a daemon thread so it doesn't block shutdown
    opener = threading.Thread(target=open_browser_when_ready, args=(BASE_URL, 15.0, 0.25), daemon=True)
    opener.start()

    return server_thread, opener

if __name__ == "__main__":
    try:
        start_server_and_browser()
        # Keep main thread alive. Server runs in background thread.
        print(f"服务器启动中，UI 地址：{BASE_URL}")
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("收到中断信号，正在退出...")
        sys.exit(0)
    except Exception as e:
        print("运行时出错:", e)
        raise