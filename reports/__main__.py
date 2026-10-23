"""CLI: ``python -m reports [--warehouse PATH] [--publish DIR] [--out DIR]``.

Builds the static site (``site/index.html`` and friends). Opens straight from file://, no server:
Plotly is bundled into ``site/assets``. Fails if any page links to a file that does not exist.
"""

from __future__ import annotations

import argparse
import logging
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import duckdb

from reports import PUBLISH_DIR, ROOT, SITE_DIR, WAREHOUSE_PATH
from reports.build import render_site
from reports.data import load

log = logging.getLogger("reports")


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("href", "src") and value:
                self.refs.append(value)


def broken_links(site: Path) -> list[str]:
    """Every local href/src that does not resolve to a file (anchors and external URLs ignored)."""
    bad = []
    for page in site.rglob("*.html"):
        parser = _Links()
        parser.feed(page.read_text(encoding="utf-8"))
        for ref in parser.refs:
            url = urlparse(ref)
            if url.scheme or ref.startswith("#"):
                continue
            if not (page.parent / url.path).resolve().is_file():
                bad.append(f"{page.relative_to(site)} -> {ref}")
    return bad


def run(warehouse: Path, publish_dir: Path, out: Path) -> list[Path]:
    if not (publish_dir / "manifest.json").exists():
        raise SystemExit(f"publish output missing in {publish_dir}; run python -m publish first")
    with duckdb.connect(str(warehouse), read_only=True) as con:
        data = load(con, ROOT, publish_dir)
        # Area plug-ins (reports/<area>/) get the same read-only connection; packets come from publish.
        pages = render_site(data, out, con=con, publish_dir=publish_dir)
    bad = broken_links(out)
    if bad:
        raise SystemExit("broken links:\n  " + "\n  ".join(bad))
    log.info("built %d pages in %s (links ok); open %s", len(pages), out, (out / "index.html").as_uri())
    return pages


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m reports", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--publish", type=Path, default=PUBLISH_DIR)
    ap.add_argument("--out", type=Path, default=SITE_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    run(args.warehouse, args.publish, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
