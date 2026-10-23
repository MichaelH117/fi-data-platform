"""CLI: ``python -m ingest [--full-refresh] [--sources SOURCE ...] [--tables ...] [--intake DIR]``.

CDC from Admin Core and the legacy admin system, plus the raw landing files and the CRM / admin-API
intake drops, into ``raw_*`` (sources: admin_core, legacy_admin, network_feed, crm, admin_api).
Exit code 1 if any table failed (its watermark is not advanced; re-run after fixing).
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from generator import ADMIN_CORE_PATH, INTAKE_DIR, LANDING_DIR, LEGACY_DB_PATH
from ingest import ADMIN_CORE_CONTRACT, LEGACY_CONTRACT, WAREHOUSE_PATH
from ingest.load import SOURCES, run


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest", description=__doc__.split("\n\n")[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--admin-core", type=Path, default=ADMIN_CORE_PATH)
    ap.add_argument("--legacy", type=Path, default=LEGACY_DB_PATH)
    ap.add_argument("--landing", type=Path, default=LANDING_DIR)
    ap.add_argument("--intake", type=Path, default=INTAKE_DIR, help="CRM export + admin-API drop root")
    ap.add_argument("--sources", nargs="+", choices=SOURCES, default=list(SOURCES))
    ap.add_argument("--tables", nargs="*", help="subset of tables (Admin Core / legacy)")
    ap.add_argument("--full-refresh", action="store_true", help="drop the raw schemas and reload everything")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    t0 = time.perf_counter()
    results = run(args.warehouse, args.admin_core, args.legacy, args.landing, ADMIN_CORE_CONTRACT, LEGACY_CONTRACT,
                  tuple(args.sources), args.full_refresh, args.tables, args.intake)
    failed = [f"{r.source}.{r.table}" for r in results if r.status != "ok"]
    logging.getLogger("ingest").info(
        "loaded %d rows, quarantined %d, tombstoned %d in %.1fs; failed tables: %s",
        sum(r.rows_loaded for r in results), sum(r.rows_quarantined for r in results),
        sum(r.rows_deleted for r in results), time.perf_counter() - t0, failed or "none",
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
