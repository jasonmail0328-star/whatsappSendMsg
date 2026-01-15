# app/tasks.py
import asyncio
import traceback
from typing import Dict, List, Tuple
from . import db, worker, config
from .logging_config import logger
import time

ADD_TASKS: Dict[str, Dict] = {}
SEND_TASKS: Dict[str, Dict] = {}
BULK_TASKS: Dict[str, Dict] = {}
BULK_CANCELS: Dict[str, asyncio.Event] = {}

# Global concurrency semaphore
_send_semaphore = asyncio.Semaphore(int(getattr(config, "MAX_CONCURRENT_SENDS", 2)))

def make_status_struct(status: str, result: dict = None, error: str = None, trace: str = None) -> Dict:
    return {"status": status, "result": result or {}, "error": error or "", "trace": trace or ""}

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

async def schedule_send_message(session_id: str, account_id: str, profile_path: str, message: str, dry_run: bool):
    global _send_semaphore
    SEND_TASKS[session_id] = make_status_struct("queued")

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

def cancel_bulk(session_id: str):
    ev = BULK_CANCELS.get(session_id)
    if not ev:
        ev = asyncio.Event()
        BULK_CANCELS[session_id] = ev
    ev.set()

async def schedule_bulk_send(session_id: str, count: int, per_account: bool, message: str, dry_run: bool, account_delay: float = None, round_delay: float = None):
    if account_delay is None:
        account_delay = float(getattr(config, "DEFAULT_ACCOUNT_INTERVAL", 1.0))
    if round_delay is None:
        round_delay = float(getattr(config, "DEFAULT_ROUND_INTERVAL", 5.0))

    BULK_TASKS[session_id] = make_status_struct("queued", result={"requested_count": count, "results": []})
    try:
        BULK_TASKS[session_id] = make_status_struct("running", result={"requested_count": count, "results": []})
        rows = db.list_accounts()
        accounts: List[Tuple[str, str]] = []
        for r in rows:
            if len(r) >= 2:
                account_id = r[0]
                profile_path = r[1]
                accounts.append((account_id, profile_path))

        if not accounts:
            BULK_TASKS[session_id] = make_status_struct("failed", error="no_accounts")
            logger.warning("BULK failed %s no accounts", session_id)
            return {"ok": False, "err": "no_accounts"}

        results = []

        # New semantics:
        # per_account == True: send exactly one message per account (legacy)
        # per_account == False: treat count as number of rounds; do count rounds across all accounts
        if per_account:
            rounds = 1
            total_to_send = len(accounts)
        else:
            rounds = int(count) if count > 0 else 0
            total_to_send = len(accounts) * rounds

        sent_count = 0
        round_idx = 0

        # Ensure cancellation event exists
        BULK_CANCELS[session_id] = BULK_CANCELS.get(session_id, asyncio.Event())

        def cancelled():
            ev = BULK_CANCELS.get(session_id)
            return ev.is_set() if ev else False

        while round_idx < rounds:
            if cancelled():
                logger.info("BULK %s cancelled before round %s", session_id, round_idx + 1)
                BULK_TASKS[session_id] = make_status_struct("cancelled", result={"requested_count": sent_count, "results": results})
                return {"ok": False, "err": "cancelled", "requested": sent_count, "results": results}
            for acc_idx, (accid, prof) in enumerate(accounts):
                if sent_count >= total_to_send:
                    break
                child_sid = f"{session_id}_r{round_idx+1}_a{acc_idx+1}"
                try:
                    r = await schedule_send_message(child_sid, accid, prof, message, dry_run)
                    results.append({"child_session": child_sid, "account": accid, "result": r})
                    BULK_TASKS[session_id]["result"]["results"] = results
                except Exception as e:
                    logger.exception("Bulk child send exception %s", e)
                    results.append({"child_session": child_sid, "account": accid, "error": str(e)})
                    BULK_TASKS[session_id]["result"]["results"] = results

                sent_count += 1
                try:
                    await asyncio.sleep(float(account_delay))
                except Exception:
                    await asyncio.sleep(1.0)
            round_idx += 1
            if round_idx < rounds:
                try:
                    await asyncio.sleep(float(round_delay))
                except Exception:
                    await asyncio.sleep(1.0)

        BULK_TASKS[session_id]["result"]["requested_count"] = total_to_send
        any_ok = any((item.get("result") and item["result"].get("ok") is True) for item in results)
        if any_ok:
            BULK_TASKS[session_id] = make_status_struct("done", result={"requested_count": total_to_send, "results": results})
        else:
            BULK_TASKS[session_id] = make_status_struct("failed", result={"requested_count": total_to_send, "results": results}, error="no_success")
        return {"ok": True, "results": results}
    except Exception as e:
        BULK_TASKS[session_id] = make_status_struct("error", error=str(e), trace=traceback.format_exc())
        logger.exception("BULK exception %s", e)
        return {"ok": False, "err": str(e)}