"""Refresh the README screenshots in docs/img/ from a built report site.

The site (``site/``) is git-ignored build output, so the README embeds a few committed screenshots
instead. This script re-takes them with a locally installed Chromium browser (Edge or Chrome) in
headless mode. It reads local files only; nothing goes to the network.

    python tools/screenshots.py              # after python -m pipeline.run
    python tools/screenshots.py --dry-run    # print the plan

Every page shot here is built from synthetic data.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SITE = REPO_ROOT / "site"
IMG_DIR = REPO_ROOT / "docs" / "img"

# (output file, page under site/, window width x height). Heights crop each page to its key section.
SHOTS: list[tuple[str, str, str]] = [
    ("start-here.png", "start.html", "1400,1000"),
    ("how-data-flows.png", "how-data-flows.html", "1400,1000"),
    ("executive-overview.png", "index.html", "1400,1100"),
    ("funded-reserve-filing.png", "regulatory/funded-reserve-tx.html", "1400,1250"),
    ("dealer-packet.png", "dealers/PVA/D1001/index.html", "1400,1180"),
    ("ops-gates-reconciliation.png", "ops.html", "1400,2180"),
]

BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def find_browser() -> str | None:
    env = os.environ.get("SCREENSHOT_BROWSER")
    if env:
        return env
    for name in ("msedge", "google-chrome", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return next((c for c in BROWSER_CANDIDATES if Path(c).exists()), None)


def command(browser: str, page: Path, out: Path, size: str) -> list[str]:
    return [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--virtual-time-budget=8000",
            f"--window-size={size}", f"--screenshot={out}", page.resolve().as_uri()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", type=Path, default=SITE)
    parser.add_argument("--out", type=Path, default=IMG_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    missing = [p for _, p, _ in SHOTS if not (args.site / p).exists()]
    if missing and not args.dry_run:
        print(f"screenshots: site pages missing ({', '.join(missing)}); run python -m pipeline.run first")
        return 2
    browser = find_browser() or "<browser>"
    if browser == "<browser>" and not args.dry_run:
        print("screenshots: no Edge/Chrome found; set SCREENSHOT_BROWSER")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    for name, page, size in SHOTS:
        cmd = command(browser, args.site / page, args.out / name, size)
        if args.dry_run:
            print(" ".join(cmd))
            continue
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
        print(f"screenshots: wrote {args.out / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
