# -*- coding: utf-8 -*-
"""SQLite 连接与建表。履约状态全部保存在本地 SQLite 文件中。"""
import os
import sqlite3

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("FULFILL_DATA_DIR", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "fulfill.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  role       TEXT NOT NULL CHECK(role IN ('warehouse','frontdesk','student')),
  student_id TEXT
);

CREATE TABLE IF NOT EXISTS textbooks (
  code        TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  cover_color TEXT NOT NULL DEFAULT '#8c3b2e'
);

CREATE TABLE IF NOT EXISTS textbook_editions (
  textbook_code TEXT NOT NULL REFERENCES textbooks(code),
  edition       TEXT NOT NULL,                 -- 版次，如 '1' / '2'
  label         TEXT NOT NULL,                 -- 展示名，如 第2版
  released_on   TEXT NOT NULL,
  diff_note     TEXT NOT NULL DEFAULT '',      -- 与上一版的差异（文字说明，不抹历史）
  PRIMARY KEY (textbook_code, edition)
);

CREATE TABLE IF NOT EXISTS classes (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  start_date TEXT NOT NULL                    -- 开课时间 ISO 日期
);

CREATE TABLE IF NOT EXISTS class_materials (
  class_id      TEXT NOT NULL REFERENCES classes(id),
  textbook_code TEXT NOT NULL,
  edition       TEXT NOT NULL,                 -- 当前计划版次；升级后改写，已寄记录不动
  qty           INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (class_id, textbook_code)
);

CREATE TABLE IF NOT EXISTS students (
  id    TEXT PRIMARY KEY,
  name  TEXT NOT NULL,
  phone TEXT
);

CREATE TABLE IF NOT EXISTS enrollments (
  id               TEXT PRIMARY KEY,
  student_id       TEXT NOT NULL REFERENCES students(id),
  class_id         TEXT NOT NULL REFERENCES classes(id),
  status           TEXT NOT NULL DEFAULT 'enrolled'
                   CHECK(status IN ('enrolled','withdrawn')),
  receiver_name    TEXT,
  receiver_phone   TEXT,
  receiver_address TEXT,
  enrolled_on      TEXT NOT NULL,
  withdrawn_on     TEXT
);

CREATE TABLE IF NOT EXISTS inventory (
  textbook_code TEXT NOT NULL,
  edition       TEXT NOT NULL,
  qty           INTEGER NOT NULL CHECK(qty >= 0),
  PRIMARY KEY (textbook_code, edition)
);

CREATE TABLE IF NOT EXISTS stock_movements (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  textbook_code TEXT NOT NULL,
  edition       TEXT NOT NULL,
  change        INTEGER NOT NULL,
  reason        TEXT NOT NULL,
  ref           TEXT DEFAULT '',
  actor         TEXT DEFAULT '',
  at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parcels (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id    TEXT NOT NULL REFERENCES enrollments(id),
  kind             TEXT NOT NULL
                   CHECK(kind IN ('first','resend','supplement')),
  status           TEXT NOT NULL
                   CHECK(status IN ('packed','shipped','delivered',
                                    'rejected','returned','cancelled')),
  exception_id     INTEGER,
  tracking_no      TEXT,
  receiver_name    TEXT,
  receiver_phone   TEXT,
  receiver_address TEXT,
  created_by       TEXT DEFAULT '',
  created_at       TEXT,
  shipped_at       TEXT,
  closed_at        TEXT
);
-- 一个报名记录的首次发放行全局唯一：取消也只保留这一行，不能再建第二个 first
CREATE UNIQUE INDEX IF NOT EXISTS idx_parcel_first
  ON parcels(enrollment_id) WHERE kind = 'first';
-- 一张异常单最多产生一个补发包裹（补发幂等）
CREATE UNIQUE INDEX IF NOT EXISTS idx_parcel_exception
  ON parcels(exception_id) WHERE exception_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS parcel_items (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  parcel_id    INTEGER NOT NULL REFERENCES parcels(id),
  textbook_code TEXT NOT NULL,
  edition      TEXT NOT NULL,                  -- 寄出时的版次快照，永不回写
  qty          INTEGER NOT NULL,
  is_supplement INTEGER NOT NULL DEFAULT 0,
  item_title   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS exceptions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
  parcel_id     INTEGER NOT NULL REFERENCES parcels(id),
  type          TEXT NOT NULL
                CHECK(type IN ('wrong_sent','rejected','damaged')),
  reason        TEXT NOT NULL,                -- 对外理由（学员可见）
  internal_note TEXT NOT NULL DEFAULT '',     -- 仓库内部备注（学员端不可见）
  status        TEXT NOT NULL DEFAULT 'submitted'
                CHECK(status IN ('submitted','approved','rejected')),
  idem_key      TEXT NOT NULL,
  submitted_by  TEXT NOT NULL,
  submitted_at  TEXT,
  approved_by   TEXT,
  approved_at   TEXT,
  resolution    TEXT DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_exc_idem ON exceptions(idem_key);

CREATE TABLE IF NOT EXISTS address_requests (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
  new_receiver  TEXT,
  new_phone     TEXT,
  new_address   TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','approved','rejected')),
  submitted_by  TEXT,
  submitted_at  TEXT,
  handled_by    TEXT,
  handled_at    TEXT,
  note          TEXT DEFAULT ''
);

-- 版次升级后，对“已寄旧版”的报名逐人生成补充资料；历史包裹原样保留
CREATE TABLE IF NOT EXISTS supplements (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  class_id      TEXT NOT NULL REFERENCES classes(id),
  enrollment_id TEXT NOT NULL REFERENCES enrollments(id),
  textbook_code TEXT NOT NULL,
  old_edition   TEXT NOT NULL,
  new_edition   TEXT NOT NULL,
  title         TEXT NOT NULL,
  note          TEXT DEFAULT '',
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','issued')),
  parcel_id     INTEGER,
  created_at    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_supplement_once
  ON supplements(enrollment_id, textbook_code, old_edition, new_edition);

CREATE TABLE IF NOT EXISTS tracking_events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  parcel_id INTEGER NOT NULL REFERENCES parcels(id),
  event     TEXT NOT NULL,
  detail    TEXT DEFAULT '',
  at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


def get_conn(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def reset_db(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    for suffix in ("", "-wal", "-shm"):
        p = path + suffix
        if os.path.exists(p):
            os.remove(p)
    conn = get_conn(path)
    init_db(conn)
    return conn
