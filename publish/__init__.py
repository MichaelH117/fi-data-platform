"""Data-product layer: aggregate-only, suppressed peer benchmarks (no contract or VIN rows) and
per-dealer packets filtered through the user -> dealer, tenant access map."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
PUBLISH_DIR = Path(os.environ.get("PUBLISH_DIR", ROOT / "data" / "publish"))
PACKETS_DIR = Path(os.environ.get("PACKETS_DIR", PUBLISH_DIR / "dealers"))
