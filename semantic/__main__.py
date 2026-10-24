"""CLI: ``python -m semantic [--target snowflake|postgres] [--out DIR] [--check] [--refresh-columns]``.

``python -m semantic export-docs [--out DIR]`` (alias ``export``) writes the Tabular-Editor-style CSV
documentation instead; see ``semantic/export.py``.

Builds ``semantic/model/`` (TMDL-style Sales / Claims / Finance / Compliance models) from
``dbt/models/metrics.yml``. Column names and types come from ``semantic/columns.lock.json``, so the
build is deterministic and works without a warehouse.

When the warehouse exists the lock is checked against it: a removed or retyped column fails the step
(the model would break on refresh); new columns are only a warning. ``--refresh-columns`` rewrites the
lock from the warehouse. ``--check`` regenerates into a temp folder and exits 1 when the committed
files differ (CI guard against hand edits or a stale model).

``--target`` picks the M source of every partition: ``snowflake`` (default; ``semantic/model/``) or
``postgres`` (``PostgreSQL.Database`` on the local server; ``semantic/model_postgres/``). Measures, calc
group, relationships and RLS are identical. ``--check`` checks the folder of the chosen target.
"""

from __future__ import annotations

import argparse
import filecmp
import logging
import os
import sys
import tempfile
from pathlib import Path

import duckdb

from semantic.build import (
    DEFAULT_TARGET,
    LOCK_PATH,
    ROOT,
    TARGET_OUT_DIRS,
    TARGETS,
    drift,
    generate,
    load_lock,
    read_warehouse_columns,
    write_lock,
)
from semantic.metrics import TranslationError

WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
log = logging.getLogger("semantic")


def _same_tree(a: Path, b: Path) -> list[str]:
    diffs = []
    files_a = {p.relative_to(a) for p in a.rglob("*") if p.is_file()}
    files_b = {p.relative_to(b) for p in b.rglob("*") if p.is_file()} if b.exists() else set()
    diffs += [f"missing: {p}" for p in sorted(files_a - files_b)]
    diffs += [f"extra: {p}" for p in sorted(files_b - files_a)]
    diffs += [f"changed: {p}" for p in sorted(files_a & files_b) if not filecmp.cmp(a / p, b / p, shallow=False)]
    return diffs


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("export-docs", "export"):  # Tabular-Editor-style CSV docs (semantic/export.py)
        from semantic.export import main as export_main
        return export_main(argv[1:])
    ap = argparse.ArgumentParser(prog="python -m semantic", description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=TARGETS, default=DEFAULT_TARGET,
                    help="M source of the partitions (default: %(default)s)")
    ap.add_argument("--out", type=Path, help="output folder (default: the target's folder under semantic/)")
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed model is stale")
    ap.add_argument("--refresh-columns", action="store_true", help="rewrite columns.lock.json from the warehouse")
    args = ap.parse_args(argv)
    args.out = args.out or TARGET_OUT_DIRS[args.target]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")

    if args.refresh_columns:
        with duckdb.connect(str(args.warehouse), read_only=True) as con:
            write_lock(read_warehouse_columns(con))
        log.info("column lock refreshed from %s", args.warehouse)

    try:
        if args.check:
            with tempfile.TemporaryDirectory() as tmp:
                generate(Path(tmp) / "model", target=args.target)
                diffs = _same_tree(Path(tmp) / "model", args.out)
            if diffs:
                log.error("semantic model is stale (run python -m semantic --target %s):\n  %s", args.target,
                          "\n  ".join(diffs[:20]))
                return 1
            log.info("semantic model is up to date")
            return 0
        plan = generate(args.out, target=args.target)
    except TranslationError as exc:
        log.error("cannot generate the semantic model: %s", exc)
        return 1

    if args.warehouse.exists():
        with duckdb.connect(str(args.warehouse), read_only=True) as con:
            live = read_warehouse_columns(con)
        errors, warnings = drift(load_lock(LOCK_PATH), live, plan)
        for w in warnings:
            log.warning("%s (python -m semantic --refresh-columns)", w)
        if errors:
            log.error("warehouse no longer matches the semantic model:\n  %s", "\n  ".join(errors))
            return 1
    n_measures = sum(len(dm.measures) for dm in plan.models.values())
    log.info("wrote %d domain models, %d measures (%d distinct metrics), %d time variants via the calc group -> %s",
             len(plan.models), n_measures, len(plan.measures), len(plan.time_variants), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
