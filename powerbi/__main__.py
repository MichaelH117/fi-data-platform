"""CLI: ``python -m powerbi [--check] [--validate] [--offline]``.

Writes the Power BI project next to this file: ``VSC Portfolio.pbip``, ``VSC Portfolio.Report/`` (PBIR) and
``VSC Portfolio.SemanticModel/`` (TMDL, PostgreSQL source). ``--check`` exits 1 when the committed files
differ from a fresh build. ``--validate`` checks every JSON file against Microsoft's published schemas
and every visual field against the model (``powerbi.validate``). Desktop's own ``.pbi/`` folders
(local settings, data cache) are never touched.
"""

from __future__ import annotations

import argparse
import logging
import sys

from powerbi.build import build_files, check, write
from powerbi.validate import validate_project

log = logging.getLogger("powerbi")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m powerbi", description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 when the committed project is stale")
    ap.add_argument("--validate", action="store_true", help="schema + field-reference validation")
    ap.add_argument("--offline", action="store_true", help="use cached schemas only (skip when missing)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    files = build_files()
    if args.check:
        diffs = check(files)
        if diffs:
            log.error("Power BI project is stale (run python -m powerbi):\n  %s", "\n  ".join(diffs[:20]))
            return 1
        log.info("Power BI project is up to date (%d files)", len(files))
    else:
        n = write(files)
        log.info("wrote %d files (%d changed)", len(files), n)
    if args.validate:
        problems, skipped = validate_project(offline=args.offline)
        for s in skipped:
            log.warning("schema not available, not validated: %s", s)
        if problems:
            log.error("%d validation problems:\n  %s", len(problems), "\n  ".join(problems[:50]))
            return 1
        log.info("validation passed: JSON schemas and visual field references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
