"""The legacy warehouse, rebuilt on purpose (docs/V3_DESIGN.md section 8).

``python -m legacy build`` creates a deliberately messy PostgreSQL database from the SAME synthetic raw
rows the modern pipeline ingests (DuckDB ``raw_admin_core`` / ``raw_legacy_admin``), so every difference
the benchmark measures is explained by structure, not by data:

- ``vsc_legacy``: source-named schemas (``legacy_ops``, ``legacy_finance``, ``legacy_reporting``), a
  tenant mirror schema (``tenant_b``), a frozen ``report`` schema and a personal sandbox
  (``zz_sandbox_analyst``). Quoted CamelCase columns, ``tbl_*`` tables under views over views, and no
  primary key, foreign key or comment anywhere.
- ``vsc_legacy_admin``: the admin system's custom-field EAV store (``admin_eav``). It is a second
  database, so it cannot be joined to the warehouse in one query.

This is the anti-pattern, kept to measure the modern platform against. It is opt-in: nothing in the
default pipeline or CI needs Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DDL_DIR = Path(__file__).with_name("ddl")
SEMANTIC_DIR = Path(__file__).with_name("semantic_models")
STATE_DIR = Path(os.environ.get("LEGACY_STATE_DIR", ROOT / "data" / "legacy"))
PLAYBOOK_DIR = ROOT / "sql" / "playbook"
BENCHMARK_JSON = ROOT / "site" / "data" / "legacy_benchmark.json"

DEFAULT_DATABASE = "vsc_legacy"
DEFAULT_ADMIN_DATABASE = "vsc_legacy_admin"
SCHEMAS = ("legacy_ops", "legacy_finance", "legacy_reporting", "tenant_b", "report", "zz_sandbox_analyst")
ADMIN_SCHEMA = "admin_eav"
TENANT_B = 2  # the second white-label tenant, mirrored as its own schema


def databases(env: dict[str, str] | None = None) -> tuple[str, str]:
    """(main, admin) database names: PG_LEGACY_DATABASE / PG_LEGACY_ADMIN_DATABASE, else the defaults."""
    from warehouse.postgres.sync import load_env

    e = load_env() if env is None else env
    return (e.get("PG_LEGACY_DATABASE") or DEFAULT_DATABASE,
            e.get("PG_LEGACY_ADMIN_DATABASE") or DEFAULT_ADMIN_DATABASE)
