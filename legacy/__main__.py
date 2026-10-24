"""python -m legacy build | demo | playbook | status | drop | semantic

    build      create vsc_legacy + vsc_legacy_admin from the DuckDB raw layer (needs PostgreSQL)
    demo       build if missing, run the benchmark pairs both ways, write site/data/legacy_benchmark.json
    playbook   run sql/playbook/manifest.yml (timeout per file), check row counts, write the results table
    status     show what is built (databases, schemas, object and row counts)
    drop       drop both legacy databases
    semantic   regenerate legacy/semantic_models/ (no PostgreSQL needed)

Exit codes: 0 ok, 1 a check failed, 2 PostgreSQL (or an input) is not available.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger("legacy")


def _pg_or_exit() -> int | None:
    from warehouse.postgres.sync import available

    why = available()
    if why:
        print(f"legacy: PostgreSQL is not available ({why}). Start it with tools/pg.ps1 start.", file=sys.stderr)
        return 2
    return None


def cmd_build(args) -> int:
    if (rc := _pg_or_exit()) is not None:
        return rc
    from legacy import build

    try:
        m = build.build(Path(args.warehouse) if args.warehouse else None)
    except FileNotFoundError as exc:
        print(f"legacy: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"database": m["database"], "admin_database": m["admin_database"], "as_of": m["as_of"],
                      "timings": m["timings"]}, indent=2))
    return 0


def cmd_status(args) -> int:
    if (rc := _pg_or_exit()) is not None:
        return rc
    from legacy import status

    print(json.dumps(status.status(), indent=2, default=str))
    return 0


def cmd_drop(args) -> int:
    if (rc := _pg_or_exit()) is not None:
        return rc
    from legacy import build

    print("dropped:", ", ".join(build.drop()) or "nothing")
    return 0


def cmd_playbook(args) -> int:
    if (rc := _pg_or_exit()) is not None:
        return rc
    from legacy import playbook

    return playbook.main(Path(args.manifest) if args.manifest else None)


def cmd_demo(args) -> int:
    if (rc := _pg_or_exit()) is not None:
        return rc
    from legacy import benchmark, build

    if args.rebuild or not build.exists():
        build.build(Path(args.warehouse) if args.warehouse else None)
    return benchmark.main(out=Path(args.out) if args.out else None)


def cmd_semantic(args) -> int:
    from legacy import semantic

    for p in semantic.write():
        print(p)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m legacy", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build both legacy databases")
    b.add_argument("--warehouse", help="DuckDB warehouse to read raw_* from (default: WAREHOUSE_PATH)")
    b.set_defaults(fn=cmd_build)
    d = sub.add_parser("demo", help="build if missing, then run the benchmark")
    d.add_argument("--warehouse")
    d.add_argument("--rebuild", action="store_true", help="rebuild even when the database exists")
    d.add_argument("--out", help="benchmark JSON path (default: site/data/legacy_benchmark.json)")
    d.set_defaults(fn=cmd_demo)
    p = sub.add_parser("playbook", help="run the analyst SQL playbook")
    p.add_argument("--manifest", help="default: sql/playbook/manifest.yml")
    p.set_defaults(fn=cmd_playbook)
    sub.add_parser("status", help="what is built").set_defaults(fn=cmd_status)
    sub.add_parser("drop", help="drop both legacy databases").set_defaults(fn=cmd_drop)
    sub.add_parser("semantic", help="regenerate legacy/semantic_models/").set_defaults(fn=cmd_semantic)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
