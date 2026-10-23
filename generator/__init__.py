"""Synthetic source systems for a VSC administrator: Admin Core (OLTP), legacy admin, landing feed.

Paths come from env vars (defaults relative to the repo root):
``ADMIN_CORE_PATH``, ``LEGACY_DB_PATH`` (``SOURCE_DB_PATH`` is a retired alias), ``LANDING_DIR``,
``INTAKE_DIR`` (CRM export + admin-API drops; default: ``intake`` next to the landing folder).
"""

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _path(var: str, default: Path, alias: str | None = None) -> Path:
    value = os.environ.get(var) or (os.environ.get(alias) if alias else None)
    path = Path(value) if value else default
    return path if path.is_absolute() else ROOT / path


ADMIN_CORE_PATH = _path("ADMIN_CORE_PATH", ROOT / "data" / "source" / "admin_core.db")
LEGACY_DB_PATH = _path("LEGACY_DB_PATH", ROOT / "data" / "source" / "legacy_admin.db", alias="SOURCE_DB_PATH")
LANDING_DIR = _path("LANDING_DIR", ROOT / "data" / "landing")
INTAKE_DIR = _path("INTAKE_DIR", LANDING_DIR.parent / "intake")
SOURCE_PATH = LEGACY_DB_PATH  # V1 name, kept for one release


def read_source_epoch(db: Path) -> dict | None:
    """The ``_source_epoch`` row a build stamps into each source DB ({epoch, seed, as_of, built_at}),
    or None when the file or the table does not exist (an older build). ``pipeline.run`` can compare
    ``seed`` with ``--seed``; ingest compares ``epoch`` with the one it loaded last."""
    if not Path(db).exists():
        return None
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT epoch, seed, as_of, built_at FROM _source_epoch LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return dict(zip(("epoch", "seed", "as_of", "built_at"), row, strict=True)) if row else None
