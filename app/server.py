# app/server.py
import asyncio
import uuid
import threading
from datetime import datetime
import traceback
import html as pyhtml
import os
import shutil

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import db, tasks, ui
from . import config as app_config
from .logging_config import logger

ROOT_URL = getattr(app_config, "ROOT_URL", "/")
HOST = getattr(app_config, "HOST", "127.0.0.1")
PORT = getattr(app_config, "PORT", 8000)
BASE_DIR = getattr(app_config, "BASE_DIR", os.path.abspath("."))

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

def _safe_remove_profile_dir(profile_path: str) -> bool:
    """
    Remove profile directory if it is under BASE_DIR/accounts and exists.
    Return True if removed or did not exist, False if refused or error.
    """
    try:
        accounts_root = os.path.join(str(BASE_DIR), "accounts")
        # Normalize paths
        profile_abs = os.path.abspath(profile_path)
        accounts_root_abs = os.path.abspath(accounts_root)
        if not profile_abs.startswith(accounts_root_abs + os.sep) and profile_abs != accounts_root_abs:
            logger.warning("Refusing to remove profile outside accounts dir: %s", profile_abs)
            return False
        if os.path.exists(profile_abs):
            shutil.rmtree(profile_abs)
            logger.info("Removed profile directory: %s", profile_abs)
        return True
    except Exception as e:
        logger.exception("Failed to remove profile dir %s: %s", profile_path, e)
        return False

def _prune_missing_profiles():
    """
    Remove DB entries whose profile_path does not exist on disk.
    Called at server startup to keep DB and filesystem in sync when user manually deletes folders.
    """
    try:
        rows = db.list_accounts()
        removed = []
        for r in rows:
            try:
                account_id = r[0]
                profile_path = r[1] if len(r) > 1 else None
                if profile_path:
                    if not os.path.exists(profile_path):
                        logger.info("Pruning missing profile for account=%s path=%s", account_id, profile_path)
                        # Prefer db.delete_account if available
                        if hasattr(db, "delete_account"):
                            try:
                                db.delete_account(account_id)
                                removed.append(account_id)
                                continue
                            except Exception:
                                logger.exception("db.delete_account failed for %s", account_id)
                        # Fallback: delete from accounts table directly if possible
                        try:
                            conn = db.get_conn()
                            cur = conn.cursor()
                            cur.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
                            conn.commit()
                            conn.close()
                            removed.append(account_id)
                        except Exception:
                            logger.exception("Fallback delete_from_db failed for %s", account_id)
            except Exception:
                logger.exception("Prune loop exception for row: %s", r)
        if removed:
            logger.info("Pruned %d missing account(s): %s", len(removed), removed)
    except Exception as e:
        logger.exception("Prune missing profiles failed: %s", e)

def create_app() -> FastAPI:
    app = FastAPI()
    db.init_db()

    # Prune missing profiles on startup so UI reflects actual filesystem state
    try:
        _prune_missing_profiles()
    except Exception:
        logger.exception("Initial prune failed")

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

    @app.get("/bulk_status/{sid}")
    async def bulk_status(sid: str):
        s = tasks.BULK_TASKS.get(sid)
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
        # read optional interval params (in seconds)
        account_delay = payload.get("account_delay")
        round_delay = payload.get("round_delay")
        try:
            account_delay = float(account_delay) if account_delay is not None else None
        except Exception:
            account_delay = None
        try:
            round_delay = float(round_delay) if round_delay is not None else None
        except Exception:
            round_delay = None

        if not message:
            return JSONResponse({"error": "message required"}, status_code=400)
        sid = "bulk_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        try:
            tasks.BULK_TASKS[sid] = tasks.make_status_struct("queued", result={"requested_count": count, "results": []})
        except Exception:
            tasks.BULK_TASKS[sid] = {"status": "queued", "result": {"requested_count": count, "results": []}, "error": "", "trace": ""}

        # Pass the optional delays through
        asyncio.create_task(tasks.schedule_bulk_send(sid, count, per_account, message, dry_run, account_delay=account_delay, round_delay=round_delay))
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

    # New: delete account endpoint used by UI
    @app.post("/delete_account")
    async def delete_account(req: Request):
        payload = await req.json()
        account_id = payload.get("account_id")
        remove_profile = bool(payload.get("remove_profile"))
        if not account_id:
            return JSONResponse({"error": "account_id required"}, status_code=400)
        profile_path = db.get_account_profile(account_id)
        if not profile_path:
            # Ensure DB entry removed if profile missing but record exists
            # If record truly not present, return not_found
            rows = db.list_accounts()
            exists_in_db = any(r[0] == account_id for r in rows)
            if not exists_in_db:
                return JSONResponse({"error": "account not found"}, status_code=404)
        # Delete profile dir if requested
        if remove_profile and profile_path:
            ok = _safe_remove_profile_dir(profile_path)
            if not ok:
                return JSONResponse({"error": "remove_profile_failed"}, status_code=500)
        # Remove account record from DB
        try:
            if hasattr(db, "delete_account"):
                db.delete_account(account_id)
            else:
                # fallback: delete from accounts table if present
                try:
                    conn = db.get_conn()
                    cur = conn.cursor()
                    cur.execute("DELETE FROM accounts WHERE account_id = ?", (account_id,))
                    conn.commit()
                    conn.close()
                except Exception:
                    logger.exception("Fallback DB delete failed for %s", account_id)
            return JSONResponse({"status": "deleted", "account_id": account_id})
        except Exception as e:
            logger.exception("delete_account failed for %s: %s", account_id, e)
            return JSONResponse({"error": "delete_failed", "detail": str(e)}, status_code=500)

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