"""SQLite schema and connection helpers for the textbook fulfillment system.

All fulfillment state lives here. Signature images live on the local
filesystem (see data/signatures/) and are referenced by filename only.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 人员：仓库 / 前台 / 学员
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('warehouse','frontdesk','student'))
);

-- 教材：同一 isbn 串起不同版次（版次升级保留历史，不删旧记录）
CREATE TABLE IF NOT EXISTS textbooks (
    id           INTEGER PRIMARY KEY,
    isbn         TEXT NOT NULL,
    title        TEXT NOT NULL,
    edition      TEXT NOT NULL,            -- 如 第1版 / 第2版
    edition_seq  INTEGER NOT NULL,         -- 数值版次，便于比较
    cover_color  TEXT NOT NULL,            -- 教材封面色
    supersedes_id INTEGER REFERENCES textbooks(id),  -- 本版替代的上一版
    UNIQUE (isbn, edition_seq)
);

-- 班级：开课时间 + 该班使用的教材版次清单
CREATE TABLE IF NOT EXISTS classes (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    start_date  TEXT NOT NULL              -- ISO 日期
);

CREATE TABLE IF NOT EXISTS class_books (
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    book_id     INTEGER NOT NULL REFERENCES textbooks(id),
    PRIMARY KEY (class_id, book_id)
);

-- 库存按「教材具体版次」记录
CREATE TABLE IF NOT EXISTS inventory (
    book_id    INTEGER PRIMARY KEY REFERENCES textbooks(id),
    qty        INTEGER NOT NULL DEFAULT 0 CHECK (qty >= 0)
);
CREATE TABLE IF NOT EXISTS inventory_log (
    id        INTEGER PRIMARY KEY,
    book_id   INTEGER NOT NULL REFERENCES textbooks(id),
    change_qt INTEGER NOT NULL,            -- 正=入库/退回，负=拣货
    reason    TEXT NOT NULL,
    ref_type  TEXT,                        -- shipment / adjustment
    ref_id    INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 报名记录
CREATE TABLE IF NOT EXISTS enrollments (
    id           INTEGER PRIMARY KEY,
    student_id   INTEGER NOT NULL REFERENCES users(id),
    class_id     INTEGER NOT NULL REFERENCES classes(id),
    enroll_date  TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','withdrawn')),
    withdraw_date TEXT
);

-- 地址快照 + 改址申请。交运前可改；交运后申请只能作用于「后续补发」。
CREATE TABLE IF NOT EXISTS addresses (
    id            INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    recipient     TEXT NOT NULL,
    phone         TEXT NOT NULL,
    address       TEXT NOT NULL,
    is_current    INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS address_requests (
    id            INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    address_id    INTEGER NOT NULL REFERENCES addresses(id),  -- 目标地址
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','approved','rejected')),
    applies_to    TEXT NOT NULL DEFAULT 'pending_packages'
                  CHECK (applies_to IN ('pending_packages','future_resends')),
    decided_by    INTEGER REFERENCES users(id),
    decided_at    TEXT,
    note          TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 包裹（一个报名记录的首次发放 initial 全局唯一）
CREATE TABLE IF NOT EXISTS shipments (
    id            INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    kind          TEXT NOT NULL CHECK (kind IN ('initial','resend')),
    status        TEXT NOT NULL DEFAULT 'planned'
                  CHECK (status IN ('planned','picking','shipped',
                                    'delivered','signed','returned',
                                    'cancelled')),
    address_snapshot TEXT NOT NULL,       -- 交运地址快照，历史不可改
    address_id    INTEGER REFERENCES addresses(id),
    tracking_no   TEXT UNIQUE,
    shipped_at    TEXT,
    delivered_at  TEXT,
    signed_at     TEXT,
    returned_at   TEXT,
    cancel_reason TEXT,
    idempotency_key TEXT UNIQUE,          -- 补发幂等键
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
-- 首次发放唯一性：一个报名记录至多一条 initial
CREATE UNIQUE INDEX IF NOT EXISTS ux_initial_shipment
    ON shipments(enrollment_id) WHERE kind = 'initial';

CREATE TABLE IF NOT EXISTS shipment_items (
    id          INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    book_id     INTEGER NOT NULL REFERENCES textbooks(id),
    qty         INTEGER NOT NULL DEFAULT 1,
    picked      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (shipment_id, book_id)
);

-- 版次升级后的补充资料（旧版已寄，不抹历史，只追加补发）
CREATE TABLE IF NOT EXISTS edition_supplements (
    id            INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    old_book_id   INTEGER NOT NULL REFERENCES textbooks(id),
    new_book_id   INTEGER NOT NULL REFERENCES textbooks(id),
    shipment_id   INTEGER REFERENCES shipments(id),  -- 补发发出后回填
    note          TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 异常申请：补发 / 错寄 / 拒收 / 破损；退课走 withdrawals
CREATE TABLE IF NOT EXISTS exception_requests (
    id            INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL REFERENCES enrollments(id),
    kind          TEXT NOT NULL
                  CHECK (kind IN ('wrong_book','rejected_logistics',
                                  'damaged','duplicate_resend')),
    reason        TEXT NOT NULL,
    evidence      TEXT,                    -- 证据说明/图路径
    status        TEXT NOT NULL DEFAULT 'submitted'
                  CHECK (status IN ('submitted','approved','rejected','closed')),
    submitted_by  INTEGER NOT NULL REFERENCES users(id),
    approved_by   INTEGER REFERENCES users(id),
    approved_at   TEXT,
    resend_shipment_id INTEGER REFERENCES shipments(id),
    idempotency_key TEXT UNIQUE,          -- 防重复补发
    warehouse_note TEXT,                  -- 仓库内部备注（学员端不可见）
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 退课记录（交运前取消未发包裹；已发出的保留物流轨迹，停止后续补发）
CREATE TABLE IF NOT EXISTS withdrawals (
    id            INTEGER PRIMARY KEY,
    enrollment_id INTEGER NOT NULL UNIQUE REFERENCES enrollments(id),
    requested_by  INTEGER NOT NULL REFERENCES users(id),
    reason        TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 物流事件（模拟接口回传）
CREATE TABLE IF NOT EXISTS tracking_events (
    id          INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    event       TEXT NOT NULL,            -- picked_up / in_transit / out_for_delivery / delivered / signed / return
    location    TEXT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 签收：本地文件放签收图
CREATE TABLE IF NOT EXISTS signatures (
    id          INTEGER PRIMARY KEY,
    shipment_id INTEGER NOT NULL UNIQUE REFERENCES shipments(id),
    signer      TEXT NOT NULL,
    image_file  TEXT NOT NULL,            -- data/signatures/ 下文件名
    signed_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
