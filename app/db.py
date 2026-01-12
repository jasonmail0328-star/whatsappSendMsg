# app/db.py (modified)
import sqlite3
from pathlib import Path
from typing import Optional, List, Tuple
from .config import DB_PATH
from .logging_config import logger

def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS accounts (
      account_id TEXT PRIMARY KEY,
      profile_path TEXT NOT NULL,
      phone TEXT,
      enabled INTEGER DEFAULT 1,
      status TEXT DEFAULT 'enabled',
      daily_limit INTEGER DEFAULT 10,
      today_sent INTEGER DEFAULT 0,
      last_used_time DATETIME,
      last_error TEXT,
      consecutive_failures INTEGER DEFAULT 0,
      in_use INTEGER DEFAULT 0,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS contacts (
      contact_id TEXT PRIMARY KEY,
      name TEXT,
      jid TEXT,
      status TEXT DEFAULT 'new',
      last_contacted_at DATETIME,
      metadata TEXT
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_jid ON contacts(jid);

    CREATE TABLE IF NOT EXISTS message_log (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      account_id TEXT,
      contact_id TEXT,
      contact_jid TEXT,
      send_time DATETIME DEFAULT CURRENT_TIMESTAMP,
      message TEXT,
      template_id TEXT,
      result TEXT,
      error TEXT,
      last_message TEXT
    );

    CREATE TABLE IF NOT EXISTS templates (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE,
      content TEXT,
      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()
    logger.info("Initialized database at %s", DB_PATH)

# Accounts
def upsert_account(account_id: str, profile_path: str, phone: Optional[str]):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO accounts(account_id, profile_path, phone, enabled, status, created_at, updated_at)
            VALUES (?, ?, ?, 1, 'enabled', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id) DO UPDATE SET profile_path=excluded.profile_path, phone=excluded.phone, updated_at=CURRENT_TIMESTAMP;
        """, (account_id, profile_path, phone))
        conn.commit()
        conn.close()
        logger.info("Upserted account %s profile=%s phone=%s", account_id, profile_path, phone)
    except Exception as e:
        logger.exception("upsert_account failed: %s", e)
        raise

def list_accounts() -> List[Tuple]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT account_id, profile_path, phone, status, today_sent, last_used_time, in_use FROM accounts ORDER BY created_at DESC;")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_account_profile(account_id: str) -> Optional[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT profile_path FROM accounts WHERE account_id=? LIMIT 1;", (account_id,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None

def set_account_in_use(account_id: str, in_use: int):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE accounts SET in_use=?, updated_at=CURRENT_TIMESTAMP WHERE account_id=?;", (1 if in_use else 0, account_id))
        conn.commit()
        conn.close()
        logger.debug("Set account %s in_use=%s", account_id, in_use)
    except Exception as e:
        logger.exception("set_account_in_use failed: %s", e)
        raise

def is_account_in_use(account_id: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT in_use FROM accounts WHERE account_id=? LIMIT 1;", (account_id,))
    row = cur.fetchone()
    conn.close()
    return bool(row[0]) if row else False

# New: atomic attempt to set in_use (returns True if we successfully locked it)
def set_account_in_use_atomic(account_id: str) -> bool:
    """
    Atomically set in_use=1 only if it was 0. Returns True if successful, False if already in use.
    """
    conn = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("BEGIN;")
        cur.execute("UPDATE accounts SET in_use=1, updated_at=CURRENT_TIMESTAMP WHERE account_id=? AND in_use=0;", (account_id,))
        changed = cur.rowcount
        conn.commit()
        conn.close()
        logger.debug("Attempted atomic set_account_in_use %s -> changed=%s", account_id, changed)
        return bool(changed)
    except Exception as e:
        logger.exception("set_account_in_use_atomic failed: %s", e)
        try:
            if conn:
                conn.rollback()
                conn.close()
        except Exception:
            pass
        raise

# Contacts and logs (unchanged)
def upsert_contact(contact_id: str, name: str, jid: str, metadata: Optional[str] = None):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO contacts(contact_id, name, jid, metadata)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(contact_id) DO UPDATE SET name=excluded.name, jid=excluded.jid, metadata=excluded.metadata;
        """, (contact_id, name, jid, metadata))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.exception("upsert_contact failed: %s", e)
        raise

def log_message(account_id: str, contact_id: str, contact_jid: str, message: str, template_id: Optional[int], result: str, error: Optional[str] = None):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO message_log(account_id, contact_id, contact_jid, message, template_id, result, error, last_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (account_id, contact_id, contact_jid, message, template_id, result, error, message))
        if result == "sent":
            cur.execute("UPDATE accounts SET today_sent = COALESCE(today_sent,0)+1 WHERE account_id=?;", (account_id,))
        conn.commit()
        conn.close()
        logger.info("log_message account=%s contact_jid=%s result=%s", account_id, contact_jid, result)
    except Exception as e:
        logger.exception("log_message failed: %s", e)
        raise

def bulk_insert_contacts(contact_list):
    if not contact_list:
        return
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("BEGIN;")
        for contact_id, name, jid, metadata in contact_list:
            cur.execute("""
                INSERT INTO contacts(contact_id, name, jid, metadata)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(contact_id) DO UPDATE SET name=excluded.name, jid=excluded.jid, metadata=excluded.metadata;
            """, (contact_id, name, jid, metadata))
        conn.commit()
        conn.close()
        logger.debug("bulk_insert_contacts inserted %d contacts", len(contact_list))
    except Exception as e:
        logger.exception("bulk_insert_contacts failed: %s", e)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        raise

def bulk_insert_messages(msg_list):
    if not msg_list:
        return
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("BEGIN;")
        for account_id, contact_id, contact_jid, message, template_id, result, error in msg_list:
            cur.execute("""
                INSERT INTO message_log(account_id, contact_id, contact_jid, message, template_id, result, error, last_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (account_id, contact_id, contact_jid, message, template_id, result, error, message))
            if result == "sent":
                cur.execute("UPDATE accounts SET today_sent = COALESCE(today_sent,0)+1 WHERE account_id=?;", (account_id,))
        conn.commit()
        conn.close()
        logger.debug("bulk_insert_messages inserted %d messages", len(msg_list))
    except Exception as e:
        logger.exception("bulk_insert_messages failed: %s", e)
        try:
            conn.rollback()
            conn.close()
        except Exception:
            pass
        raise

def get_all_contact_jids() -> List[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT contact_jid FROM message_log;")
    rows = [r[0] for r in cur.fetchall() if r and r[0]]
    conn.close()
    return rows

def list_templates():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, name, content, created_at FROM templates ORDER BY created_at DESC;")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_template(name: str, content: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO templates(name, content) VALUES (?, ?);", (name, content))
    conn.commit()
    conn.close()

def delete_template(name: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM templates WHERE name=?;", (name,))
    conn.commit()
    conn.close()

def delete_account(account_id: str, remove_messages: bool = False):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("DELETE FROM accounts WHERE account_id=?;", (account_id,))
        if remove_messages:
            cur.execute("DELETE FROM message_log WHERE account_id=?;", (account_id,))
        conn.commit()
        conn.close()
        logger.info("delete_account %s remove_messages=%s", account_id, remove_messages)
    except Exception as e:
        logger.exception("delete_account failed: %s", e)
        raise

def update_account_usage(account_id: str, sent_inc: int = 0):
    try:
        conn = get_conn()
        cur = conn.cursor()
        if sent_inc:
            cur.execute("UPDATE accounts SET today_sent = COALESCE(today_sent,0)+?, last_used_time=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE account_id=?;", (sent_inc, account_id))
        else:
            cur.execute("UPDATE accounts SET last_used_time=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE account_id=?;", (account_id,))
        conn.commit()
        conn.close()
        logger.debug("update_account_usage %s sent_inc=%s", account_id, sent_inc)
    except Exception as e:
        logger.exception("update_account_usage failed: %s", e)
        raise