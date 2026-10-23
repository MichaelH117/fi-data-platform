"""Ingest: landing-feed intake (``python -m ingest.feed``) and CDC replication into raw (``python -m ingest``)."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _path(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    path = Path(value) if value else default
    return path if path.is_absolute() else ROOT / path


WAREHOUSE_PATH = _path("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb")
CONTRACTS_DIR = ROOT / "contracts"
LEGACY_CONTRACT = CONTRACTS_DIR / "legacy_admin.yml"
ADMIN_CORE_CONTRACT = CONTRACTS_DIR / "admin_core.yml"
CONTRACT_PATH = LEGACY_CONTRACT  # V1 name, kept for one release
