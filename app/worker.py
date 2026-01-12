# app/worker.py
"""
异步 Worker：负责与 Playwright 交互
- add_account_task_async: 创建 profile、等待扫码、检测 account info 并写入 DB
- detect_account_info_with_retries: 多次检测 phone/jid/pushname
- fetch_visible_contacts_async: 滚动抓取聊天列表联系人（返回列表）
- send_message_on_page_async: 在已打开 page 上执行搜索并发送消息
- select_and_send_async: 在异步上下文中打开 persistent context，抓取联系人、选择未触达目标并发送消息，返回结果 dict
"""
import asyncio
import hashlib
import re
import time
import random
import json
from pathlib import Path
from typing import Dict, Optional, List

from .config import LOGIN_TIMEOUT, PAGE_TIMEOUT, QR_DETECT_RETRIES
from . import db
from .logging_config import logger

# -------------------- Helpers --------------------
def contact_id_from_jid_or_name(jid: Optional[str], name: str) -> str:
    if jid:
        return jid
    return "namehash_" + hashlib.sha1((name or "").encode("utf-8")).hexdigest()

# -------------------- detect (with retries) --------------------
async def detect_account_info_with_retries(page, retries: int = QR_DETECT_RETRIES, delay: float = 0.8) -> Dict[str, Optional[str]]:
    """
    多次尝试从 window.Store 或页面 DOM 中提取 phone/jid/pushname。
    返回 {"phone":..., "jid":..., "displayName":...}
    """
    info = {"phone": None, "jid": None, "displayName": None}
    for attempt in range(1, max(1, retries) + 1):
        try:
            res = await page.evaluate("""() => {
                try {
                    if (!window || !window.Store) return null;
                    if (window.Store.Me) {
                        return {
                            id: window.Store.Me.id || window.Store.Me._serialized || null,
                            pushname: window.Store.Me.pushname || null,
                            number: window.Store.Me.__x_formatted || window.Store.Me.number || null
                        }
                    }
                    if (window.Store.Conn && window.Store.Conn.me) {
                        return { id: window.Store.Conn.me };
                    }
                    return null;
                } catch(e) { return null; }
            }""")
            if res:
                info["jid"] = res.get("id")
                info["phone"] = res.get("number")
                info["displayName"] = res.get("pushname")
                logger.debug("detect attempt %d got store info: %s", attempt, {"jid": info["jid"], "phone": info["phone"]})
                if info["jid"] or info["phone"]:
                    return info
        except Exception as e:
            logger.debug("detect attempt %d store evaluate exception: %s", attempt, e)

        # fallback: 打开 header/profile 并扫描可见文本寻找电话
        try:
            try:
                await page.click("header div[role='button']", timeout=2000)
            except Exception:
                try:
                    await page.click("header img", timeout=2000)
                except Exception:
                    pass
            await page.wait_for_timeout(300)
            texts = await page.evaluate("() => Array.from(document.querySelectorAll('div, span')).map(n=>n.innerText).filter(Boolean)")
            phone_pattern = re.compile(r'(\+\d{6,}|\d{6,})')
            for t in texts:
                if not t:
                    continue
                m = phone_pattern.search(t)
                if m:
                    info["phone"] = m.group(0)
                    logger.debug("detect attempt %d found phone via DOM: %s", attempt, info["phone"])
                    break
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            if info["phone"]:
                return info
        except Exception as e:
            logger.debug("detect attempt %d DOM scan exception: %s", attempt, e)

        await asyncio.sleep(delay)

    logger.info("detect_account_info_with_retries finished, result=%s", info)
    return info

# -------------------- add account --------------------
async def add_account_task_async(session_id: str, profile_name: str):
    """
    创建 profile，打开 WhatsApp Web 并等待扫码登录，检测 account 信息并写入 DB。
    返回 dict: { success: bool, account_id, phone, jid, profile_path, reason? }
    """
    from playwright.async_api import async_playwright
    profile_path = Path("accounts") / profile_name
    profile_path.mkdir(parents=True, exist_ok=True)
    profile_abs = str(profile_path.resolve())
    logger.info("ADD task %s starting, profile=%s", session_id, profile_abs)

    try:
        async with async_playwright() as pw:
            browser_type = pw.chromium
            browser = await browser_type.launch_persistent_context(user_data_dir=profile_abs, headless=False, args=["--start-maximized"])
            page = await browser.new_page()
            await page.goto("https://web.whatsapp.com", wait_until="networkidle")
            logged = False
            elapsed = 0
            check_interval = 2
            while elapsed < LOGIN_TIMEOUT:
                try:
                    grid = await page.query_selector("div[role='grid']")
                    if grid:
                        logged = True
                        break
                    has_store = await page.evaluate("() => !!(window && window.Store && (window.Store.Me || (window.Store.Conn && window.Store.Conn.me)))")
                    if has_store:
                        logged = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(check_interval)
                elapsed += check_interval

            if not logged:
                logger.warning("ADD task %s login timeout after %s seconds", session_id, LOGIN_TIMEOUT)
                try:
                    await browser.close()
                except Exception:
                    pass
                return {"success": False, "reason": "login_timeout", "profile_path": profile_abs}

            # 登录成功后，使用 detect 封装多次检测
            info = await detect_account_info_with_retries(page, retries=QR_DETECT_RETRIES, delay=0.8)
            key = info.get("phone") or info.get("jid") or profile_abs
            account_id = "acc_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

            try:
                db.upsert_account(account_id, profile_abs, info.get("phone"))
            except Exception as e:
                logger.exception("ADD task %s db.upsert_account failed: %s", session_id, e)
                try:
                    await browser.close()
                except Exception:
                    pass
                return {"success": False, "reason": "db_write_failed", "profile_path": profile_abs, "error": str(e)}

            try:
                await browser.close()
            except Exception:
                pass

            logger.info("ADD task %s registered account=%s phone=%s profile=%s", session_id, account_id, info.get("phone"), profile_abs)
            return {"success": True, "account_id": account_id, "phone": info.get("phone"), "jid": info.get("jid"), "profile_path": profile_abs}
    except Exception as e:
        logger.exception("ADD task %s exception: %s", session_id, e)
        return {"success": False, "reason": "exception", "error": str(e)}

# -------------------- fetch visible contacts (async, with scrolling) --------------------
async def fetch_visible_contacts_async(page, limit: int = 500) -> List[Dict]:
    """
    滚动抓取聊天列表联系人，返回 list of {"name":..., "jid":..., "contact_id":...}
    """
    contacts = []
    seen = set()

    container_selectors = [
        "div[role='grid']",
        "div[aria-label='聊天']",
        "div._1ays2",
        "div[role='region']"
    ]

    container = None
    for sel in container_selectors:
        try:
            container = await page.query_selector(sel)
            if container:
                logger.debug("Found container selector: %s", sel)
                break
        except Exception:
            continue

    async def collect_rows(rows):
        nonlocal contacts, seen
        for r in rows:
            try:
                name_el = await r.query_selector("span[dir='auto']")
                name = (await name_el.inner_text()).strip() if name_el else ""
                if not name:
                    continue
            except Exception:
                continue
            jid = None
            try:
                a = await r.query_selector("a")
                if a:
                    for attr in ("data-id", "data-jid", "dataJid", "href", "aria-label"):
                        try:
                            val = await a.get_attribute(attr)
                            if val:
                                jid = val
                                break
                        except Exception:
                            continue
            except Exception:
                pass
            contact_id = contact_id_from_jid_or_name(jid, name)
            if contact_id not in seen:
                contacts.append({"name": name, "jid": jid or contact_id, "contact_id": contact_id})
                seen.add(contact_id)
                if len(contacts) >= limit:
                    break

    if not container:
        # fallback 抓取第一页行
        rows = await page.query_selector_all("div[role='row']")
        await collect_rows(rows)
        logger.info("fetch_visible_contacts_async fallback rows=%d", len(contacts))
        return contacts

    max_scrolls = 12
    scroll_pause = 0.6
    last_count = 0

    for i in range(max_scrolls):
        rows = await container.query_selector_all("div[role='row']")
        await collect_rows(rows)
        if len(contacts) >= limit:
            break
        # scroll container
        try:
            await page.evaluate("(el) => { el.scrollBy(0, 600); }", container)
        except Exception:
            try:
                await page.evaluate("(el) => { el.scrollTop = el.scrollTop + 600; }", container)
            except Exception:
                await page.evaluate("window.scrollBy(0, 600)")
        await asyncio.sleep(scroll_pause)
        if len(contacts) == last_count:
            if i > max_scrolls // 2:
                break
        last_count = len(contacts)

    logger.info("fetch_visible_contacts_async collected %d contacts", len(contacts))
    return contacts

# -------------------- send on page --------------------
async def send_message_on_page_async(page, target_name: str, message: str, dry_run: bool = False, timeout: int = 60):
    """
    在已打开的 page 上搜索 target_name 并发送 message。
    返回 (success:bool, error_str_or_None)
    """
    try:
        await page.wait_for_selector("div[role='grid']", timeout=timeout * 1000)
    except Exception:
        return False, "not_logged_in_or_no_chats"

    await asyncio.sleep(random.uniform(0.8, 1.6))
    search_sel = "div[contenteditable='true'][data-tab='3']"
    try:
        await page.wait_for_selector(search_sel, timeout=15000)
    except Exception:
        search_sel = "div[role='textbox'][contenteditable='true']"
    search = await page.query_selector(search_sel)
    if not search:
        return False, "search_box_not_found"
    await search.click()
    await asyncio.sleep(random.uniform(0.1, 0.4))
    try:
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
    except Exception:
        pass
    await asyncio.sleep(0.2)
    for ch in target_name:
        await page.keyboard.type(ch)
        await asyncio.sleep(random.uniform(0.02, 0.12))
    await asyncio.sleep(random.uniform(0.8, 1.6))
    try:
        first = await page.query_selector("//span[@title]")
        if not first:
            return False, "no_search_result"
        await first.click()
    except Exception as e:
        return False, f"click_result_failed:{e}"
    await asyncio.sleep(random.uniform(0.6, 1.2))
    input_sel = "footer div[contenteditable='true']"
    try:
        await page.wait_for_selector(input_sel, timeout=15000)
    except Exception:
        return False, "input_box_not_found"
    input_box = await page.query_selector(input_sel)
    await input_box.click()
    await asyncio.sleep(random.uniform(0.2, 0.5))
    if dry_run:
        return True, None
    try:
        import pyperclip
        pyperclip.copy(message)
        await page.keyboard.press("Control+v")
        await asyncio.sleep(random.uniform(0.2, 0.5))
    except Exception:
        for ch in message:
            await page.keyboard.type(ch)
            await asyncio.sleep(random.uniform(0.02, 0.12))
    await asyncio.sleep(random.uniform(0.2, 0.5))
    await page.keyboard.press("Enter")
    await asyncio.sleep(random.uniform(0.6, 1.2))
    return True, None

# -------------------- select and send --------------------
async def select_and_send_async(account_id: str, profile_path: str, message: str, dry_run: bool = False, timeout: int = 60):
    """
    主流程（异步）：
    - 打开 persistent context（profile_path）
    - 抓取可见联系人（滚动）
    - bulk 插入 contacts 到 DB
    - 选择未触达的联系人（全局去重），选中后发送
    - 记录 message_log（使用 db.log_message）
    返回 result dict:
      { ok: bool, result: 'sent'|'simulated'|'failed', err: str?, target: {name,jid}, contacts: [...] }
    """
    from playwright.async_api import async_playwright
    profile_abs = str(Path(profile_path).resolve())
    logger.info("select_and_send_async start account=%s profile=%s dry_run=%s", account_id, profile_abs, dry_run)
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch_persistent_context(user_data_dir=profile_abs, headless=False, args=["--start-maximized"])
            try:
                page = await browser.new_page()
                try:
                    await page.goto("https://web.whatsapp.com", wait_until="networkidle", timeout=timeout * 1000)
                except Exception:
                    logger.warning("select_and_send_async page goto failed for account=%s", account_id)
                    return {"ok": False, "err": "page_load_timeout_or_not_logged_in"}
                try:
                    await page.wait_for_selector("div[role='grid']", timeout=timeout * 1000)
                except Exception:
                    logger.warning("select_and_send_async no chats for account=%s", account_id)
                    return {"ok": False, "err": "not_logged_in_or_no_chats"}

                contacts = await fetch_visible_contacts_async(page, limit=500)
                if not contacts:
                    logger.info("select_and_send_async no visible contacts for account=%s", account_id)
                    return {"ok": False, "err": "no_visible_contacts", "contacts": []}

                # bulk insert contacts into DB (transaction)
                try:
                    contact_tuples = [(c["contact_id"], c["name"], c["jid"], json.dumps({})) for c in contacts]
                    db.bulk_insert_contacts(contact_tuples)
                except Exception as e:
                    logger.exception("bulk_insert_contacts failed: %s", e)

                # build summary for logging/return
                contacts_summary = [{"name": c["name"], "jid": c["jid"]} for c in contacts]
                logger.info("[send] account=%s fetched %d contacts", account_id, len(contacts_summary))
                for ci in contacts_summary[:15]:
                    logger.debug("  contact: %s (%s)", ci["name"], ci["jid"])

                # choose not-yet-contacted (global)
                conn = db.get_conn()
                cur = conn.cursor()
                cur.execute("SELECT contact_jid FROM message_log;")
                sent = set([r[0] for r in cur.fetchall() if r and r[0]])
                conn.close()
                available = [c for c in contacts if c["jid"] not in sent]
                if not available:
                    logger.info("select_and_send_async no_available_uncontacted for account=%s", account_id)
                    return {"ok": False, "err": "no_available_uncontacted", "contacts": contacts_summary}

                chosen = random.choice(available)
                logger.info("[send] account=%s chosen target: %s (%s)", account_id, chosen["name"], chosen["jid"])

                success, err = await send_message_on_page_async(page, chosen["name"], message, dry_run=dry_run, timeout=timeout)
                if success:
                    res = "simulated" if dry_run else "sent"
                    try:
                        db.log_message(account_id, chosen["contact_id"], chosen["jid"], message, template_id=None, result=res, error=None)
                        # update usage - done in tasks layer (but still safe to call)
                    except Exception as e:
                        logger.exception("log_message failed after send: %s", e)
                    return {"ok": True, "result": res, "target": {"name": chosen["name"], "jid": chosen["jid"]}, "contacts": contacts_summary}
                else:
                    try:
                        db.log_message(account_id, chosen["contact_id"], chosen["jid"], message, template_id=None, result="failed", error=err)
                    except Exception:
                        logger.exception("log_message failed for failed send")
                    logger.warning("[send] account=%s send failed err=%s", account_id, err)
                    return {"ok": False, "err": err, "target": {"name": chosen["name"], "jid": chosen["jid"]}, "contacts": contacts_summary}
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
    except Exception as e:
        logger.exception("select_and_send_async exception for account=%s: %s", account_id, e)
        return {"ok": False, "err": "exception", "trace": str(e)}