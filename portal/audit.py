"""Portal access log: one row per export or API read (who, which scope and dataset, how many rows).

Admin Core is opened read-only by the portal, so the log is its own small SQLite database next to
it (``portal_access_log.db``; override with ``PORTAL_ACCESS_LOG``). Denied requests are logged too,
with ``status = 'DENIED'`` and no rows. The log never stores the data itself or the API key.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS portal_access_log (
    access_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    accessed_at   TIMESTAMP NOT NULL,
    channel       TEXT NOT NULL,      -- cli | api
    login         TEXT NOT NULL,      -- portal login, or operator:<os user> for an operator CLI export
    role          TEXT,
    tenant_code   TEXT,
    dealer_number TEXT,
    data_scope    TEXT NOT NULL,      -- standard | contact
    dataset       TEXT NOT NULL,
    row_count     INTEGER,
    since         TEXT,
    status        TEXT NOT NULL,      -- OK | DENIED
    message       TEXT
)"""
_lock = threading.Lock()


def log_path(admin_core: Path) -> Path:
    env = os.environ.get("PORTAL_ACCESS_LOG")
    return Path(env) if env else Path(admin_core).with_name("portal_access_log.db")


def record(admin_core: Path, *, channel: str, login: str, role: str | None, tenant_code: str | None,
           dealer_number: str | None, data_scope: str, dataset: str, row_count: int | None,
           since: str | None = None, status: str = "OK", message: str | None = None) -> None:
    path = log_path(admin_core)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, sqlite3.connect(path) as conn:
        conn.execute(DDL)
        conn.execute(
            "INSERT INTO portal_access_log (accessed_at, channel, login, role, tenant_code, dealer_number, "
            "data_scope, dataset, row_count, since, status, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [datetime.now().isoformat(sep=" ", timespec="seconds"), channel, login, role, tenant_code, dealer_number,
             data_scope, dataset, row_count, since, status, message],
        )
