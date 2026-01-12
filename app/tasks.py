# app/tasks.py (modified)
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

    # 获取全局并发许可（排队） —— 先获取信号量再尝试原子锁定账号，避免 race
    await _send_semaphore.acquire()
    try:
        # 原子地尝试占用 account（数据库层）
        locked = False
        try:
            locked = db.set_account_in_use_atomic(account_id)
        except Exception as e:
            # DB 错误：释放 semaphore 并失败返回
            _send_semaphore.release()
            SEND_TASKS[session_id] = make_status_struct("failed", error="db_error", trace=traceback.format_exc())
            logger.exception("SEND failed acquiring account lock due to DB error %s", e)
            return {"ok": False, "err": "db_error"}

        if not locked:
            # 已被占用
            _send_semaphore.release()
            SEND_TASKS[session_id] = make_status_struct("failed", error="account_in_use")
            logger.warning("SEND rejected %s account_in_use %s", session_id, account_id)
            return {"ok": False, "err": "account_in_use"}

        # 标记 account 为 in_use 已经做为原子操作
        SEND_TASKS[session_id] = make_status_struct("running")
        try:
            # 增加 timeout 宽限（可按需调整）
            result = await worker.select_and_send_async(account_id, profile_path, message, dry_run=dry_run, timeout=120)
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
        # 清除 account in_use 并释放并发许可（确保释放）
        try:
            db.set_account_in_use(account_id, 0)
        except Exception:
            logger.exception("set_account_in_use clear failed for %s", account_id)
        _send_semaphore.release()

# Bulk send wrapper: 支持 per_account 模式与 count 模式
# 其余逻辑保持原实现（不变）
# ... (保留原本的 bulk send 代码)