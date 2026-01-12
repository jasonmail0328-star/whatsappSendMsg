# app/tasks.py
# Updated: add schedule_bulk_send implementation and minor robustness around task tracking.

import asyncio
import hashlib
import traceback
from typing import Dict, List
from . import db, worker, config
from .logging_config import logger
import random
import time

ADD_TASKS: Dict[str, Dict] = {}
SEND_TASKS: Dict[str, Dict] = {}
BULK_TASKS: Dict[str, Dict] = {}

# Global concurrency semaphore
_send_semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_SENDS)

def make_status_struct(status: str, result: dict = None, error: str = None, trace: str = None) -> Dict:
    return {"status": status, "result": result or {}, "error": error or "", "trace": trace or ""}

# Add-account wrapper (original)
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

# Send-message wrapper (original behavior)
async def schedule_send_message(session_id: str, account_id: str, profile_path: str, message: str, dry_run: bool):
    SEND_TASKS[session_id] = make_status_struct("queued")

    # Acquire global concurrency permit first
    await _send_semaphore.acquire()
    try:
        locked = False
        try:
            locked = db.set_account_in_use_atomic(account_id)
        except Exception as e:
            _send_semaphore.release()
            SEND_TASKS[session_id] = make_status_struct("failed", error="db_error", trace=traceback.format_exc())
            logger.exception("SEND failed acquiring account lock due to DB error %s", e)
            return {"ok": False, "err": "db_error"}

        if not locked:
            _send_semaphore.release()
            SEND_TASKS[session_id] = make_status_struct("failed", error="account_in_use")
            logger.warning("SEND rejected %s account_in_use %s", session_id, account_id)
            return {"ok": False, "err": "account_in_use"}

        SEND_TASKS[session_id] = make_status_struct("running")
        try:
            result = await worker.select_and_send_async(account_id, profile_path, message, dry_run=dry_run, timeout=120)
            if result.get("ok"):
                SEND_TASKS[session_id] = make_status_struct("done", result=result)
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
        try:
            db.set_account_in_use(account_id, 0)
        except Exception:
            logger.exception("set_account_in_use clear failed for %s", account_id)
        _send_semaphore.release()

# Bulk send wrapper (new implementation)
async def schedule_bulk_send(session_id: str, count: int, per_account: bool, message: str, dry_run: bool):
    """
    Schedule a bulk sending job.
    - per_account: when True, send exactly one message per account (ignores count)
    - when False: send `count` messages, distributing across accounts round-robin
    """
    BULK_TASKS[session_id] = make_status_struct("queued", result={"requested_count": count, "results": []})
    try:
        BULK_TASKS[session_id] = make_status_struct("running", result={"requested_count": count, "results": []})
        # Load accounts
        rows = db.list_accounts()
        accounts = []
        for r in rows:
            # list_accounts returns (account_id, profile_path, phone, status, today_sent, last_used_time, in_use?)
            if len(r) >= 2:
                account_id = r[0]
                profile_path = r[1]
                accounts.append((account_id, profile_path))

        if not accounts:
            BULK_TASKS[session_id] = make_status_struct("failed", error="no_accounts")
            logger.warning("BULK failed %s no accounts", session_id)
            return {"ok": False, "err": "no_accounts"}

        child_tasks = []
        requested = 0

        if per_account:
            for (accid, prof) in accounts:
                child_sid = f"{session_id}_{accid}"
                # schedule child send as task; schedule_send_message will record SEND_TASKS for each child_sid
                t = asyncio.create_task(schedule_send_message(child_sid, accid, prof, message, dry_run))
                child_tasks.append((child_sid, t))
                requested += 1
        else:
            # distribute count across accounts round-robin
            idx = 0
            while requested < count:
                accid, prof = accounts[idx % len(accounts)]
                child_sid = f"{session_id}_{requested}"
                t = asyncio.create_task(schedule_send_message(child_sid, accid, prof, message, dry_run))
                child_tasks.append((child_sid, t))
                requested += 1
                idx += 1

        # update requested_count
        BULK_TASKS[session_id]["result"]["requested_count"] = requested

        # Wait for all child tasks, collect results incrementally
        results = []
        for sid_child, task in child_tasks:
            try:
                r = await task
                results.append({"child_session": sid_child, "result": r})
                BULK_TASKS[session_id]["result"]["results"] = results
            except Exception as e:
                logger.exception("Bulk child task exception %s", e)
                results.append({"child_session": sid_child, "error": str(e)})
                BULK_TASKS[session_id]["result"]["results"] = results

        # decide final status
        any_ok = any((res.get("result") and (res["result"].get("ok") is True)) or (res.get("result") and res["result"].get("result")=="sent") for res in results)
        if any_ok:
            BULK_TASKS[session_id] = make_status_struct("done", result={"requested_count": requested, "results": results})
        else:
            BULK_TASKS[session_id] = make_status_struct("failed", result={"requested_count": requested, "results": results}, error="no_success")
        return {"ok": True, "results": results}
    except Exception as e:
        BULK_TASKS[session_id] = make_status_struct("error", error=str(e), trace=traceback.format_exc())
        logger.exception("BULK exception %s", e)
        return {"ok": False, "err": str(e)}