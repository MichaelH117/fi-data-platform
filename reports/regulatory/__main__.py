"""CLI: ``python -m reports.regulatory [--warehouse PATH] [--out DIR]``.

Builds only the regulatory pages and workbooks (no publish output needed), rendered with the shell
template, so the area can be checked on its own. ``python -m reports`` builds them as part of the site.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from pathlib import Path

import duckdb
import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from reports import ROOT, WAREHOUSE_PATH
from reports.build import HERE, month_label
from reports.regulatory import NAV_LABEL, build

log = logging.getLogger("reports.regulatory")


def run(warehouse: Path, out: Path) -> list[Path]:
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    tpl = env.get_template("page.html.j2")
    (out / "assets").mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "static" / "site.css", out / "assets" / "site.css")
    shutil.copy(HERE / "static" / "site.js", out / "assets" / "site.js")
    (out / "assets" / "plotly.min.js").write_text("", encoding="utf-8")  # pages have no charts
    with duckdb.connect(str(warehouse), read_only=True) as con:
        pages = build(con, out)
        as_of = pd.Timestamp(con.execute("select as_of_date from core.dim_reporting_date").fetchone()[0])
    nav = [(pages[0].path, NAV_LABEL)] if pages else []
    written = []
    for page in pages:
        path = out / page.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tpl.render(page=page, root="../" * page.path.count("/"), nav=nav,
                                   active=page.nav or page.path, as_of=as_of,
                                   last_month=month_label(as_of.replace(day=1) - pd.Timedelta(days=1)),
                                   chart=lambda s: Markup(s)), encoding="utf-8")
        written.append(path)
    if pages and not (out / "index.html").exists():  # the brand link points at the site root
        (out / "index.html").write_text(f'<!doctype html><meta charset="utf-8"><title>Regulatory</title>'
                                        f'<a href="{pages[0].path}">Regulatory filings</a>', encoding="utf-8")
    log.info("built %d regulatory pages in %s", len(written), out)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reports.regulatory", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--out", type=Path, default=ROOT / "site_regulatory")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    run(args.warehouse, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
