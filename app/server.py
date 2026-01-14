# app/server.py
import asyncio
import uuid
import threading
from datetime import datetime
import traceback
import html as pyhtml

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import db, tasks, ui
from . import config as app_config
from .logging_config import logger

# Export ROOT_URL, HOST, PORT for run.py compatibility (use defaults if missing)
ROOT_URL = getattr(app_config, "ROOT_URL", "/")
HOST = getattr(app_config, "HOST", "127.0.0.1")
PORT = getattr(app_config, "PORT", 8000)

def html_escape(s: str) -> str:
    return pyhtml.escape(s)

def sanitize_settings(payload: dict) -> dict:
    allowed = {
        "MAX_PROFILE_STARTS": int,
        "PROFILE_START_MIN_DELAY": float,
        "PROFILE_START_MAX_DELAY": float,
        "BULK_POLL_INTERVAL": float,
        "ADD_POLL_INTERVAL": float,
        "SEND_POLL_INTERVAL": float,
        "STORAGE_FLUSH_WAIT": float,
        "PERSIST_VERIFY_WAIT": float,
        "PERSIST_VERIFY_RETRIES": int,
        "MAX_CONCURRENT_SENDS": int,
        "LOGIN_TIMEOUT": int,
        "QR_DETECT_RETRIES": int
    }
    out = {}
    for k, t in allowed.items():
        if k in payload:
            try:
                v = payload[k]
                nv = int(v) if t is int else float(v)
                out[k] = nv
            except Exception:
                logger.warning("Invalid setting value for %s: %s", k, payload.get(k))
    if "PROFILE_START_MIN_DELAY" in out and "PROFILE_START_MAX_DELAY" in out:
        mn = out["PROFILE_START_MIN_DELAY"]
        mx = out["PROFILE_START_MAX_DELAY"]
        if mn > mx:
            out["PROFILE_START_MIN_DELAY"], out["PROFILE_START_MAX_DELAY"] = mx, mn
    return out

def create_app() -> FastAPI:
    app = FastAPI()
    db.init_db()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        try:
            rows = db.list_accounts()
            return HTMLResponse(ui.render_main_page(rows))
        except Exception as e:
            tb = traceback.format_exc()
            logger.exception("Error rendering main page: %s", e)
            body = "<h3>Server error while rendering page</h3><pre>{}</pre>".format(html_escape(tb))
            return HTMLResponse(body, status_code=500)

    @app.post("/add")
    async def add_account_endpoint():
        sid = "s_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        profile_name = "acc_" + uuid.uuid4().hex
        try:
            tasks.ADD_TASKS[sid] = tasks.make_status_struct("queued")
        except Exception:
            tasks.ADD_TASKS[sid] = {"status": "queued", "result": {}, "error": "", "trace": ""}
        asyncio.create_task(tasks.schedule_add_account(profile_name, sid))
        return JSONResponse({"status": "queued", "session_id": sid})

    @app.get("/add_status/{sid}")
    async def add_status(sid: str):
        s = tasks.ADD_TASKS.get(sid)
        if not s:
            return JSONResponse({"status": "not_found"})
        return JSONResponse(s)

    @app.post("/send")
    async def send_endpoint(req: Request):
        payload = await req.json()
        account_id = payload.get("account_id")
        message = payload.get("message")
        dry_run = bool(payload.get("dry_run"))
        if message is None or account_id is None:
            return JSONResponse({"error": "account_id and message required"}, status_code=400)
        profile_path = db.get_account_profile(account_id)
        if not profile_path:
            return JSONResponse({"error": "account not found"}, status_code=404)
        if db.is_account_in_use(account_id):
            return JSONResponse({"error": "account is currently in use"}, status_code=409)
        sid = "send_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        asyncio.create_task(tasks.schedule_send_message(sid, account_id, profile_path, message, dry_run))
        return JSONResponse({"status": "queued", "session_id": sid})

    @app.get("/send_status/{sid}")
    async def send_status(sid: str):
        s = tasks.SEND_TASKS.get(sid)
        if not s:
            return JSONResponse({"status": "not_found"})
        return JSONResponse(s)

    @app.post("/bulk_send")
    async def bulk_send(req: Request):
        payload = await req.json()
        count = int(payload.get("count") or 0)
        per_account = bool(payload.get("per_account"))
        message = payload.get("message")
        dry_run = bool(payload.get("dry_run"))
        if not message:
            return JSONResponse({"error": "message required"}, status_code=400)
        sid = "bulk_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        try:
            tasks.BULK_TASKS[sid] = tasks.make_status_struct("queued", result={"requested_count": count, "results": []})
        except Exception:
            tasks.BULK_TASKS[sid] = {"status": "queued", "result": {"requested_count": count, "results": []}, "error": "", "trace": ""}
        asyncio.create_task(tasks.schedule_bulk_send(sid, count, per_account, message, dry_run))
        return JSONResponse({"status": "queued", "session_id": sid})

    @app.post("/bulk_cancel")
    async def bulk_cancel(req: Request):
        payload = await req.json()
        sid = payload.get("session_id")
        if not sid:
            return JSONResponse({"error": "session_id required"}, status_code=400)
        if sid not in tasks.BULK_TASKS:
            return JSONResponse({"error": "session not found"}, status_code=404)
        try:
            tasks.cancel_bulk(sid)
            tasks.BULK_TASKS[sid] = tasks.make_status_struct("cancelling", result=tasks.BULK_TASKS[sid].get("result", {}))
            return JSONResponse({"status": "cancelling", "session_id": sid})
        except Exception as e:
            logger.exception("bulk_cancel failed: %s", e)
            return JSONResponse({"error": "cancel_failed", "detail": str(e)}, status_code=500)

    return app

def run_uvicorn(app=None, host=None, port=None, reload=False, block=False):
    import uvicorn
    _app = app or create_app()
    _host = host or HOST
    _port = int(port or PORT)
    def _runner():
        uvicorn.run(_app, host=_host, port=_port, log_level="info", reload=reload)
    if block:
        _runner()
        return None
    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    return t