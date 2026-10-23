"""Static report site (Jinja2 + Plotly) built from gold, gold_actuarial and the publish layer."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
PUBLISH_DIR = Path(os.environ.get("PUBLISH_DIR", ROOT / "data" / "publish"))
SITE_DIR = Path(os.environ.get("SITE_DIR", ROOT / "site"))
