# app/tasks.py
import asyncio
import hashlib
import traceback
from typing import Dict
from . import db, worker, config
from .logging_config import logger
import random
import time

ADD_TASKS: Dict[str, Dict] = {}
SEND_TASKS: Dict[str, Dict] = {}
BULK_TASKS: Dict[str, Dict] = {}

# 全局并发信号量
_send_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SENDS)

def make_status_struct(status: str, result: dict = None, error: str = None, trace: str = None) -> Dict:
    return {"status": status, "result": result or {}, "error": error or "", "trace": trace or ""}

# Add-account wrapper
async def schedule_add_account(profile_name: str, session_id: str):
    ADD_TASKS[session_id] = make_status_struct("queued")
    try:
        ADD_TASKS[session_id] = make_status_struct("running")
        res = await worker.add_account_task_async(session_id, profile_name)
        if res.get("success"):
            ADD_TASKS[session_id] = make_status_struct("done", result=res)
            logger.info("ADD done %s %s", session_id, res)
        else:
            ADD_TASKS[session_id] = make_status_struct("failed", result=res, error=res.get("reason") or "add_failed")
            logger.warning("ADD failed %s %s", session_id, res)
    except Exception as e:
        ADD_TASKS[session_id] = make_status_struct("error", error=str(e), trace=traceback.format_exc())
        logger.exception("ADD exception %s", e)

# Send-message wrapper (use async worker.select_and_send_async)
async def schedule_send_message(session_id: str, account_id: str, profile_path: str, message: str, dry_run: bool):
    SEND_TASKS[session_id] = make_status_struct("queued")
    # 检查 account ��否已被占用
    if db.is_account_in_use(account_id):
        SEND_TASKS[session_id] = make_status_struct("failed", error="account_in_use")
        logger.warning("SEND rejected %s account_in_use %s", session_id, account_id)
        return SEND_TASKS[session_id]["result"]

    # 获取全局并发许可（排队）
    await _send_semaphore.acquire()
    try:
        # 标记 account 为 in_use
        db.set_account_in_use(account_id, 1)
        SEND_TASKS[session_id] = make_status_struct("running")
        try:
            result = await worker.select_and_send_async(account_id, profile_path, message, dry_run=dry_run, timeout=60)
            if result.get("ok"):
                SEND_TASKS[session_id] = make_status_struct("done", result=result)
                # 更新使用统计
                try:
                    db.update_account_usage(account_id, sent_inc=1 if not dry_run else 0)
                except Exception:
                    logger.exception("update_account_usage failed for %s", account_id)
                logger.info("SEND done %s account=%s target=%s", session_id, account_id, result.get("target"))
            else:
                SEND_TASKS[session_id] = make_status_struct("failed", result=result, error=result.get("err"))
                logger.warning("SEND failed %s account=%s err=%s", session_id, account_id, result.get("err"))
            return result
        except Exception as e:
            SEND_TASKS[session_id] = make_status_struct("error", error=str(e), trace=traceback.format_exc())
            logger.exception("SEND exception %s", e)
            return {"ok": False, "err": str(e)}
    finally:
        # 清除 account in_use 并释放并发许可
        try:
            db.set_account_in_use(account_id, 0)
        except Exception:
            logger.exception("set_account_in_use clear failed for %s", account_id)
        _send_semaphore.release()

# Bulk send wrapper: 支持 per_account 模式与 count 模式
async def schedule_bulk_send(session_id: str, count: int, per_account: bool, message: str, dry_run: bool):
    BULK_TASKS[session_id] = make_status_struct("queued")
    try:
        BULK_TASKS[session_id] = make_status_struct("running", result={"requested_count": count, "per_account": per_account, "results": []})
        accounts = db.list_accounts()
        if not accounts:
            BULK_TASKS[session_id] = make_status_struct("failed", error="no_accounts")
            logger.warning("BULK failed %s no accounts", session_id)
            return

        if per_account:
            tasks_list = []
            for acc in accounts:
                account_id = acc[0]
                profile_path = acc[1]
                sid = f"bulk_send_{session_id}_{account_id}"
                tasks_list.append(schedule_send_message(sid, account_id, profile_path, message, dry_run))
            results = await asyncio.gather(*tasks_list, return_exceptions=True)
            summary = []
            for idx, res in enumerate(results):
                account_id = accounts[idx][0]
                if isinstance(res, Exception):
                    summary.append({"account_id": account_id, "ok": False, "err": str(res)})
                else:
                    summary.append({"account_id": account_id, "result": res})
            BULK_TASKS[session_id] = make_status_struct("done", result={"requested_count": count, "per_account": per_account, "results": summary})
            logger.info("BULK per_account done %s", session_id)
            return

        # count mode: round-robin until total reached or no available
        total_to_send = max(0, int(count))
        sent_count = 0
        account_count = len(accounts)
        idx = 0
        attempts_no_available = 0
        while sent_count < total_to_send and attempts_no_available < account_count:
            acc = accounts[idx % account_count]
            account_id = acc[0]; profile_path = acc[1]
            sid = f"bulk_send_{session_id}_{account_id}_{sent_count}"
            result = await schedule_send_message(sid, account_id, profile_path, message, dry_run)
            BULK_TASKS[session_id]["result"]["results"].append({"account_id": account_id, "result": result})
            if result and result.get("ok"):
                sent_count += 1
                attempts_no_available = 0
            else:
                err = result.get("err") if isinstance(result, dict) else None
                if err in ("no_available_uncontacted", "no_visible_contacts"):
                    attempts_no_available += 1
                else:
                    attempts_no_available += 1
            idx += 1
        if sent_count > 0:
            BULK_TASKS[session_id] = make_status_struct("done", result=BULK_TASKS[session_id]["result"])
            logger.info("BULK done %s sent_count=%d", session_id, sent_count)
        else:
            BULK_TASKS[session_id] = make_status_struct("failed", error="no_targets")
            logger.warning("BULK failed %s no targets", session_id)
    except Exception as e:
        BULK_TASKS[session_id] = make_status_struct("error", error=str(e), trace=traceback.format_exc())
        logger.exception("BULK exception %s", e)