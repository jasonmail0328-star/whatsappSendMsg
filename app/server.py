# app/server.py
import asyncio
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from . import db, tasks, ui
from .config import ROOT_URL, HOST, PORT
import uvicorn
from datetime import datetime
import threading
import os
import time
import shutil
from .logging_config import logger

def create_app() -> FastAPI:
    app = FastAPI()
    db.init_db()

    @app.get("/", response_class=HTMLResponse)
    async def index():
        rows = db.list_accounts()
        return HTMLResponse(ui.render_main_page(rows))

    @app.post("/add")
    async def add_account_endpoint():
        sid = "s_" + datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
        profile_name = "acc_" + datetime.utcnow().strftime("%Y%m%d%H%M%S")
        asyncio.create_task(tasks.schedule_add_account(profile_name, sid))
        return JSONResponse(tasks.ADD_TASKS.get(sid) or {"status": "queued"})

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
        # schedule
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
        asyncio.create_task(tasks.schedule_bulk_send(sid, count, per_account, message, dry_run))
        return JSONResponse({"status": "queued", "session_id": sid})

    @app.get("/bulk_status/{sid}")
    async def bulk_status(sid: str):
        s = tasks.BULK_TASKS.get(sid)
        if not s:
            return JSONResponse({"status": "not_found"})
        return JSONResponse(s)

    @app.post("/delete_account")
    async def delete_account_endpoint(req: Request):
        payload = await req.json()
        account_id = payload.get("account_id")
        remove_profile = bool(payload.get("remove_profile"))
        remove_messages = bool(payload.get("remove_messages"))
        if not account_id:
            return JSONResponse({"error": "account_id required"}, status_code=400)
        profile_path = db.get_account_profile(account_id)
        if not profile_path:
            return JSONResponse({"error": "account not found"}, status_code=404)
        if db.is_account_in_use(account_id):
            return JSONResponse({"error": "account is currently in use"}, status_code=409)
        try:
            db.delete_account(account_id, remove_messages=remove_messages)
        except Exception as e:
            logger.exception("delete_account db delete failed: %s", e)
            return JSONResponse({"error": "db_delete_failed", "detail": str(e)}, status_code=500)
        if remove_profile and profile_path:
            try:
                shutil.rmtree(profile_path, ignore_errors=True)
            except Exception as e:
                logger.exception("delete_account remove_profile failed: %s", e)
                return JSONResponse({"error": "remove_profile_failed", "detail": str(e)}, status_code=500)
        logger.info("delete_account ok account=%s remove_profile=%s remove_messages=%s", account_id, remove_profile, remove_messages)
        return JSONResponse({"ok": True})

    @app.get("/accounts", response_class=HTMLResponse)
    async def accounts_page():
        rows = db.list_accounts()
        return HTMLResponse(ui.render_accounts_page(rows))

    return app

def run_uvicorn(app):
    logger.info("Starting server on %s:%s", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")