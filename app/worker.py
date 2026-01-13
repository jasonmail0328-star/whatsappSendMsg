# app/worker.py
# Corrected worker implementation (fixed syntax errors).
# Features:
# - add_account_task_async (persistent profile, wait for login)
# - detect_account_info_with_retries
# - send_message_on_page_async (slow typing using config.CHAR_DELAY_*)
# - select_and_send_async (uses persistent context if available; debug dumps)
#
# Replace your existing app/worker.py with this file (backup original first).

import asyncio
import hashlib
import traceback
import random
import time
import re
import os
import json
from pathlib import Path
from typing import Tuple, Optional, Dict, List

from playwright.async_api import async_playwright, Page, BrowserContext

from . import db, config
from .logging_config import logger


# -------------------- Helpers --------------------

def contact_id_from_jid_or_name(jid: Optional[str], name: str) -> str:
    if jid:
        return jid
    return "namehash_" + hashlib.sha1((name or "").encode("utf-8")).hexdigest()


async def detect_account_info_with_retries(page: Page, retries: int = None, delay: float = 0.8) -> Dict[str, Optional[str]]:
    """
    Try to detect account info (jid/phone/displayName) from window.Store or DOM.
    """
    if retries is None:
        retries = getattr(config, "QR_DETECT_RETRIES", 4)
    info = {"phone": None, "jid": None, "displayName": None}

    for attempt in range(1, max(1, retries) + 1):
        try:
            res = await page.evaluate(
                """() => {
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
                }"""
            )
            if res:
                info["jid"] = res.get("id")
                info["phone"] = res.get("number")
                info["displayName"] = res.get("pushname")
                logger.debug("detect attempt %d got store info: %s", attempt, {"jid": info["jid"], "phone": info["phone"]})
                if info["jid"] or info["phone"]:
                    return info
        except Exception as e:
            logger.debug("detect attempt %d store evaluate exception: %s", attempt, e)

        # fallback DOM scan for phone-like text
        try:
            try:
                await page.click("header div[role='button']", timeout=1500)
            except Exception:
                try:
                    await page.click("header img", timeout=1500)
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

async def add_account_task_async(session_id: str, profile_name: str) -> Dict:
    """
    Create profile dir, open persistent context, wait for login, detect info and upsert DB.
    Return dict describing success/failure.
    """
    profile_path = Path(config.BASE_DIR) / "accounts" / profile_name
    profile_path.mkdir(parents=True, exist_ok=True)
    profile_abs = str(profile_path.resolve())
    logger.info("ADD task %s starting, profile=%s", session_id, profile_abs)

    pw = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None

    try:
        pw = await async_playwright().start()

        # Try persistent context first (so login session persists to profile_abs)
        try:
            context = await pw.chromium.launch_persistent_context(profile_abs, headless=False)
            pages = context.pages
            page = pages[0] if pages else await context.new_page()
            logger.debug("ADD: launched persistent context for profile=%s", profile_abs)
        except Exception as e:
            logger.debug("ADD: persistent context failed (%s), fallback to ephemeral", e)
            # fallback to ephemeral context
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()

        # Navigate to WhatsApp Web
        try:
            await page.goto("https://web.whatsapp.com", wait_until="networkidle", timeout=int(getattr(config, "LOGIN_TIMEOUT", 180)) * 1000)
        except Exception as e:
            # Save debug artifacts and return error
            debug_dir = Path(config.LOGS_DIR or "logs") / "add" / session_id
            debug_dir.mkdir(parents=True, exist_ok=True)
            try:
                await page.screenshot(path=str(debug_dir / "goto_failed.png"), full_page=True)
                html = await page.content()
                with open(debug_dir / "goto_failed.html", "w", encoding="utf-8") as fh:
                    fh.write(html)
                logger.warning("ADD task %s page.goto failed, saved debug to %s", session_id, debug_dir)
            except Exception as se:
                logger.exception("ADD task %s failed saving debug artifacts: %s", session_id, se)
            logger.exception("ADD task %s page.goto exception: %s", session_id, e)
            # cleanup
            try:
                if context:
                    await context.close()
            except Exception:
                pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            return {"success": False, "reason": "login_timeout", "profile_path": profile_abs, "error": str(e)}

        # Wait for login (grid or window.Store)
        logged = False
        elapsed = 0
        check_interval = 2
        LOGIN_TIMEOUT = getattr(config, "LOGIN_TIMEOUT", 180)
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
                if context:
                    await context.close()
            except Exception:
                pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            return {"success": False, "reason": "login_timeout", "profile_path": profile_abs}

        # Detect account info
        info = await detect_account_info_with_retries(page, retries=getattr(config, "QR_DETECT_RETRIES", 4), delay=0.8)
        key = info.get("phone") or info.get("jid") or profile_abs
        account_id = "acc_" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]

        try:
            db.upsert_account(account_id, profile_abs, info.get("phone"))
        except Exception as e:
            logger.exception("ADD task %s db.upsert_account failed: %s", session_id, e)
            try:
                if context:
                    await context.close()
            except Exception:
                pass
            if pw:
                try:
                    await pw.stop()
                except Exception:
                    pass
            return {"success": False, "reason": "db_write_failed", "profile_path": profile_abs, "error": str(e)}

        # Success: close context (profile saved)
        try:
            if context:
                await context.close()
        except Exception:
            logger.exception("ADD task %s failed to close context", session_id)
        try:
            if pw:
                await pw.stop()
        except Exception:
            logger.exception("ADD task %s failed to stop playwright", session_id)

        logger.info("ADD task %s registered account=%s phone=%s profile=%s", session_id, account_id, info.get("phone"), profile_abs)
        return {"success": True, "profile_path": profile_abs, "account_id": account_id, "phone": info.get("phone")}

    except Exception as e:
        logger.exception("ADD task %s unexpected exception: %s", session_id, e)
        try:
            if context:
                await context.close()
        except Exception:
            pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        return {"success": False, "reason": "exception", "profile_path": profile_abs, "error": str(e)}


# -------------------- send on page --------------------

async def send_message_on_page_async(page: Page, target_name: str, message: str, dry_run: bool = False, timeout: int = 30) -> Tuple[bool, Optional[str]]:
    """
    On a given open page, search target_name, open chat and send message.
    Uses config.CHAR_DELAY_MIN/MAX for per-character delay.
    """
    try:
        try:
            await page.wait_for_selector("div[role='grid']", timeout=timeout * 1000)
        except Exception:
            return False, "not_logged_in_or_no_chats"

        await asyncio.sleep(random.uniform(0.8, 1.6))

        search_sel_candidates = [
            "div[contenteditable='true'][data-tab='3']",
            "div[role='textbox'][contenteditable='true']",
            "div[contenteditable='true'][data-tab='6']"
        ]
        search = None
        for sel in search_sel_candidates:
            try:
                await page.wait_for_selector(sel, timeout=4000)
                search = await page.query_selector(sel)
                if search:
                    break
            except Exception:
                continue

        if not search:
            return False, "search_box_not_found"

        await search.click()
        await asyncio.sleep(random.uniform(0.1, 0.4))

        # Clear and type target name
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

        input_sel_candidates = [
            "footer div[contenteditable='true']",
            "div[role='textbox'][contenteditable='true']"
        ]
        input_box = None
        for sel in input_sel_candidates:
            try:
                await page.wait_for_selector(sel, timeout=4000)
                input_box = await page.query_selector(sel)
                if input_box:
                    break
            except Exception:
                continue

        if not input_box:
            return False, "input_box_not_found"

        await input_box.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))

        if dry_run:
            return True, None

        char_min = getattr(config, "CHAR_DELAY_MIN", 0.05)
        char_max = getattr(config, "CHAR_DELAY_MAX", 0.18)

        # Try clipboard paste first for long messages; otherwise type char-by-char with configured delays
        try:
            import pyperclip
            pyperclip.copy(message)
            await page.keyboard.press("Control+v")
            await asyncio.sleep(random.uniform(0.2, 0.5))
        except Exception:
            for ch in message:
                await page.keyboard.type(ch)
                await asyncio.sleep(random.uniform(char_min, char_max))
        await asyncio.sleep(random.uniform(0.2, 0.5))

        try:
            await page.keyboard.press("Enter")
        except Exception:
            try:
                send_btn = await page.query_selector("button[data-testid='compose-btn-send']")
                if send_btn:
                    await send_btn.click()
                else:
                    return False, "send_action_failed"
            except Exception as e:
                return False, f"send_action_failed:{e}"

        await asyncio.sleep(random.uniform(0.6, 1.2))
        return True, None

    except Exception as e:
        logger.exception("send_message_on_page_async exception: %s", e)
        return False, f"exception:{e}"


# -------------------- select and send --------------------

async def select_and_send_async(account_id: str, profile_path: str, message: str, dry_run: bool = False, timeout: int = 120) -> Dict:
    """
    Open profile (persistent if available) or ephemeral browser, scrape contacts, pick uncontacted one and send.
    """
    try:
        Path(config.SCREENSHOTS_DIR).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    pw = None
    context: Optional[BrowserContext] = None
    page: Optional[Page] = None

    try:
        pw = await async_playwright().start()

        # Try persistent context if profile exists
        used_persistent = False
        try:
            if profile_path:
                p = Path(profile_path)
                if p.exists():
                    try:
                        context = await pw.chromium.launch_persistent_context(str(p.resolve()), headless=False)
                        pages = context.pages
                        page = pages[0] if pages else await context.new_page()
                        used_persistent = True
                        logger.debug("Launched persistent context for account=%s profile=%s", account_id, profile_path)
                    except Exception as e:
                        logger.exception("launch_persistent_context failed for %s: %s", profile_path, e)
                        context = None
                        page = None
        except Exception:
            logger.exception("Error while checking profile_path for account=%s", account_id)

        if context is None:
            browser = await pw.chromium.launch(headless=False)
            context = await browser.new_context()
            page = await context.new_page()
            logger.debug("Launched ephemeral browser for account=%s", account_id)

        # Navigate to web.whatsapp.com (best-effort)
        try:
            await page.goto("https://web.whatsapp.com", timeout=int(timeout) * 1000)
        except Exception:
            logger.debug("page.goto timed out/failed for account=%s; continuing", account_id)

        # If using persistent profile and not logged in, wait (allows user to scan)
        if used_persistent:
            logged = False
            elapsed = 0
            check_interval = 2
            LOGIN_TIMEOUT = getattr(config, "LOGIN_TIMEOUT", 180)
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
                # save debug artifacts and return
                try:
                    debug_dir = Path(config.LOGS_DIR or "logs") / account_id
                    debug_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        await page.screenshot(path=str(debug_dir / f"no_chats_{int(time.time())}.png"), full_page=True)
                        html = await page.content()
                        with open(debug_dir / f"no_chats_{int(time.time())}.html", "w", encoding="utf-8") as fh:
                            fh.write(html)
                    except Exception:
                        pass
                except Exception:
                    pass
                return {"ok": False, "err": "not_logged_in_or_no_chats"}

        # Scrape contacts (heuristic)
        contacts: List[Dict] = []
        try:
            contacts_js = """
                () => {
                    const rows = Array.from(document.querySelectorAll("div[role='row']"));
                    const out = [];
                    for (const r of rows) {
                        try {
                            const titleElem = r.querySelector("span[title]");
                            if (!titleElem) continue;
                            const name = titleElem.getAttribute("title") || titleElem.innerText || "";
                            out.push({name: name, jid: name, contact_id: name});
                        } catch (e) {}
                    }
                    return out;
                }
            """
            c = await page.evaluate(contacts_js)
            if isinstance(c, list):
                contacts = c
        except Exception:
            logger.exception("Failed to eval contacts on page for account=%s", account_id)

        if not contacts:
            logger.info("select_and_send_async no contacts scraped for account=%s", account_id)
            return {"ok": False, "err": "no_visible_contacts", "contacts": []}

        # Persist contacts
        try:
            contact_tuples = [(c["contact_id"], c["name"], c["jid"], json.dumps({})) for c in contacts]
            db.bulk_insert_contacts(contact_tuples)
        except Exception:
            logger.exception("bulk_insert_contacts failed")

        contacts_summary = [{"name": c["name"], "jid": c["jid"]} for c in contacts]

        # Determine already-sent JIDs
        try:
            conn = db.get_conn()
            cur = conn.cursor()
            cur.execute("SELECT contact_jid FROM message_log;")
            sent = set([r[0] for r in cur.fetchall() if r and r[0]])
            conn.close()
        except Exception:
            logger.exception("Failed to fetch message_log for account=%s", account_id)
            sent = set()

        available = [c for c in contacts if (c.get("jid") not in sent)]
        if not available:
            logger.info("select_and_send_async no_available_uncontacted for account=%s", account_id)
            return {"ok": False, "err": "no_available_uncontacted", "contacts": contacts_summary}

        chosen = random.choice(available)
        logger.info("[send] account=%s chosen target: %s (%s)", account_id, chosen.get("name"), chosen.get("jid"))

        success, err = await send_message_on_page_async(page, chosen.get("name"), message, dry_run=dry_run, timeout=timeout)
        if success:
            res = "simulated" if dry_run else "sent"
            try:
                db.log_message(account_id, chosen.get("contact_id"), chosen.get("jid"), message, template_id=None, result=res, error=None)
            except Exception:
                logger.exception("log_message failed after send")
            return {"ok": True, "result": res, "target": {"name": chosen.get("name"), "jid": chosen.get("jid")}, "contacts": contacts_summary}
        else:
            try:
                db.log_message(account_id, chosen.get("contact_id"), chosen.get("jid"), message, template_id=None, result="failed", error=err)
            except Exception:
                logger.exception("log_message failed for failed send")
            logger.warning("[send] account=%s send failed err=%s", account_id, err)
            return {"ok": False, "err": err, "target": {"name": chosen.get("name"), "jid": chosen.get("jid")}, "contacts": contacts_summary}

    except Exception as e:
        logger.exception("select_and_send_async exception for account=%s: %s", account_id, e)
        return {"ok": False, "err": f"exception:{e}"}

    finally:
        # Debug dumps and cleanup
        try:
            if page and os.environ.get('DEBUG_DUMP_SCREENSHOT') == '1':
                try:
                    timestamp = int(time.time())
                    fname = Path(config.SCREENSHOTS_DIR) / f"{account_id}_{timestamp}.png"
                    htmlname = Path(config.SCREENSHOTS_DIR) / f"{account_id}_{timestamp}.html"
                    try:
                        await page.screenshot(path=str(fname), full_page=True)
                    except Exception:
                        logger.exception("Failed to take debug screenshot for account=%s", account_id)
                    try:
                        html = await page.content()
                        with open(str(htmlname), "w", encoding="utf-8") as f:
                            f.write(html)
                    except Exception:
                        logger.exception("Failed to dump page HTML for account=%s", account_id)
                except Exception:
                    logger.exception("Unexpected failure during debug dump for account=%s", account_id)
            # Keep context open if requested
            if os.environ.get('DEBUG_KEEP_BROWSER') == '1':
                logger.info("DEBUG_KEEP_BROWSER=1, leaving context open for account=%s", account_id)
            else:
                try:
                    if context:
                        await context.close()
                except Exception:
                    logger.exception("Failed to close context for account=%s", account_id)
        except Exception:
            logger.exception("Cleanup failure in finally for account=%s", account_id)
        finally:
            try:
                if pw:
                    await pw.stop()
            except Exception:
                logger.exception("Failed to stop Playwright for account=%s", account_id)