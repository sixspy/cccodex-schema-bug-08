import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we drive BEGIN IMMEDIATE / COMMIT explicitly so that
    # every version-pointer change is serialized by SQLite's write lock.
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str | None = None):
    """Single write transaction: BEGIN IMMEDIATE -> work -> COMMIT.

    BEGIN IMMEDIATE takes the database write lock up front.  Two 仓管 grabbing
    the same table can no longer interleave a read-modify-write and silently
    overwrite each other's version pointer; the loser waits, re-reads, and is
    rejected by the compare-and-swap guards.
    """
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_contracts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('现行','草稿','停用')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(table_name, version_no)
);

CREATE TABLE IF NOT EXISTS contract_fields (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL REFERENCES schema_contracts(id),
  field_name TEXT NOT NULL,
  field_type TEXT NOT NULL,
  UNIQUE(contract_id, field_name)
);

CREATE TABLE IF NOT EXISTS catalog_tables (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL UNIQUE,
  live_version INTEGER NOT NULL,
  registered_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS apply_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  contract_id INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  from_version INTEGER,
  to_version INTEGER NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reconcile_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of TEXT NOT NULL,
  matched INTEGER NOT NULL,
  drifted INTEGER NOT NULL,
  summary TEXT NOT NULL,
  misrollback_open INTEGER NOT NULL DEFAULT 0,
  misrollback_closed INTEGER NOT NULL DEFAULT 0,
  verified INTEGER NOT NULL DEFAULT 1,
  details TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per rollback attempt.  Out-of-window attempts are stored as 已拦 and
-- must be closed out ("收口") with exactly one outcome: 撤回 or 留仓.
CREATE TABLE IF NOT EXISTS rollback_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL,
  from_version INTEGER NOT NULL,
  to_version INTEGER NOT NULL,
  within_window INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已回退','已拦','已撤回','已留仓')),
  outcome TEXT CHECK (outcome IS NULL OR outcome IN ('撤回','留仓')),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  resolved_at TEXT
);

-- Read-only warehouse for retained 误退 traces.  Append-only: the API exposes
-- reads only, and UNIQUE(event_id) guarantees the two outcomes cannot both be
-- attached to one event.
CREATE TABLE IF NOT EXISTS rollback_archive (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL UNIQUE REFERENCES rollback_events(id),
  table_name TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  contract_id INTEGER,
  fields_json TEXT NOT NULL DEFAULT '[]',
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED = [
    ("dw_orders_d", 1, "现行", "初始现行", [("order_id", "INT"), ("amount", "DECIMAL"), ("biz_date", "DATE")]),
    ("dw_orders_d", 2, "草稿", "拟删 amount 改币种", [("order_id", "INT"), ("currency", "STRING"), ("biz_date", "DATE")]),
    ("dw_payments_d", 1, "现行", "支付现行", [("pay_id", "INT"), ("order_id", "INT"), ("paid_at", "TIMESTAMP")]),
]

META_DEFAULTS = [
    ("rollback_window_hours", "24"),
    ("last_apply_at", "2020-01-01T00:00:00"),
]


def _ensure_columns(conn, table: str, columns: dict[str, str]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        # Lightweight forward migration for databases created by the old schema.
        _ensure_columns(
            conn,
            "reconcile_reports",
            {
                "misrollback_open": "INTEGER NOT NULL DEFAULT 0",
                "misrollback_closed": "INTEGER NOT NULL DEFAULT 0",
                "verified": "INTEGER NOT NULL DEFAULT 1",
                "details": "TEXT NOT NULL DEFAULT ''",
            },
        )
        # Meta defaults must exist even when the database was already seeded.
        for key, value in META_DEFAULTS:
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?,?)", (key, value))
        if conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone():
            return
        for table, ver, status, note, fields in SEED:
            cur = conn.execute(
                "INSERT INTO schema_contracts(table_name, version_no, status, note) VALUES (?,?,?,?)",
                (table, ver, status, note),
            )
            cid = cur.lastrowid
            for fname, ftype in fields:
                conn.execute(
                    "INSERT INTO contract_fields(contract_id, field_name, field_type) VALUES (?,?,?)",
                    (cid, fname, ftype),
                )
            if status == "现行":
                conn.execute(
                    "INSERT OR REPLACE INTO catalog_tables(table_name, live_version) VALUES (?,?)",
                    (table, ver),
                )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
