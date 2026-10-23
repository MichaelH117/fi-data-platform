"""CLI: ``python -m generator [--seed N] [--as-of YYYY-MM-DD] [--legacy N] [--contracts N] [--append-day [DAYS]]``.

Builds the Admin Core OLTP database, the legacy admin database and the latest landing drops,
or appends DAYS business days of activity (Admin Core work + a new landing drop per day).
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import date
from pathlib import Path

from generator import ADMIN_CORE_PATH, INTAKE_DIR, LANDING_DIR, LEGACY_DB_PATH
from generator import catalog as cat
from generator.build import build
from generator.daily import append_day

log = logging.getLogger("generator")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m generator", description=__doc__)
    ap.add_argument("--admin-core", type=Path, default=ADMIN_CORE_PATH, help="Admin Core SQLite path")
    ap.add_argument("--legacy", type=Path, default=LEGACY_DB_PATH, help="legacy admin SQLite path")
    ap.add_argument("--landing", type=Path, default=LANDING_DIR, help="landing folder root")
    ap.add_argument("--intake", type=Path, default=INTAKE_DIR, help="CRM export + admin-API drop root")
    ap.add_argument("--seed", type=int, default=cat.DEFAULT_SEED)
    default_as_of = date.fromisoformat(os.environ["AS_OF_DATE"]) if os.environ.get("AS_OF_DATE") else cat.DEFAULT_AS_OF
    ap.add_argument("--as-of", type=date.fromisoformat, default=default_as_of, help="last day of the initial build")
    ap.add_argument("--legacy-contracts", type=int, default=cat.DEFAULT_LEGACY_CONTRACTS)
    ap.add_argument("--contracts", type=int, default=cat.DEFAULT_CORE_CONTRACTS, help="Admin Core contracts")
    ap.add_argument("--append-day", type=int, nargs="?", const=1, default=0, metavar="DAYS",
                    help="append DAYS (default 1) days of activity to the existing sources")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    t0 = time.perf_counter()
    if args.append_day:
        if not args.admin_core.exists() or not args.legacy.exists():
            ap.error("source databases do not exist; run `python -m generator` first")
        for _ in range(args.append_day):
            log.info("appended day: %s", append_day(args.admin_core, args.legacy, args.landing, args.intake))
    else:
        counts = build(args.admin_core, args.legacy, args.landing, seed=args.seed, as_of=args.as_of,
                       n_legacy=args.legacy_contracts, n_core=args.contracts, intake_dir=args.intake)
        log.info("built sources (seed=%s, as_of=%s): %s", args.seed, args.as_of, counts)
    log.info("done in %.1fs", time.perf_counter() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
