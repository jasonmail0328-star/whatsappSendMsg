-- migrations/001_create_tables.sql
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