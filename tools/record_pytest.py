"""Record a pytest run: ``python tools/record_pytest.py [pytest args...]``.

Runs the suite in-process and writes the outcome counts to ``data/runs/_pytest.json`` (or
``$PIPELINE_RUNS_DIR/_pytest.json``), the artifact the "Python tests passed" proof tile and
``docs/showcase_stats.json`` read (reports/showcase.py). The tile never shows a count that was not
recorded here. Default arguments: ``-q -m "not slow"`` (the two end-to-end runs need a full pipeline's
memory); pass your own arguments to record a different selection, e.g. ``python tools/record_pytest.py -q``.
The node IDs of failing tests (and of setup/teardown errors) are saved as ``failed_tests`` /
``error_tests`` so a red count can be traced to the exact tests. Exit code: pytest's.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARGS = ["-q", "-m", "not slow", "-p", "no:cacheprovider"]
OUTCOMES = ("passed", "failed", "skipped", "xfailed", "xpassed")


class Recorder:
    def __init__(self) -> None:
        self.counts = dict.fromkeys(OUTCOMES, 0)
        self.errors = 0
        self.deselected = 0
        self.collected = 0
        self.failed_ids: list[str] = []
        self.error_ids: list[str] = []

    def pytest_deselected(self, items) -> None:
        self.deselected += len(items)

    def pytest_collection_finish(self, session) -> None:
        self.collected = len(session.items)

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "call" or (report.when == "setup" and report.skipped):
            key = "xfailed" if hasattr(report, "wasxfail") and report.skipped else \
                  "xpassed" if hasattr(report, "wasxfail") else report.outcome
            self.counts[key] = self.counts.get(key, 0) + 1
            if key == "failed":
                self.failed_ids.append(report.nodeid)
        elif report.failed:  # setup/teardown error
            self.errors += 1
            self.error_ids.append(f"{report.nodeid} ({report.when})")


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:]) or DEFAULT_ARGS
    rec = Recorder()
    t0 = time.perf_counter()
    code = int(pytest.main(args, plugins=[rec]))
    out = Path(os.environ.get("PIPELINE_RUNS_DIR", ROOT / "data" / "runs")) / "_pytest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {**rec.counts, "errors": rec.errors, "deselected": rec.deselected, "collected": rec.collected,
               "failed_tests": rec.failed_ids, "error_tests": rec.error_ids,
               "selection": " ".join(args), "exit_code": code, "seconds": round(time.perf_counter() - t0, 1),
               "recorded_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {payload['passed']} passed, {payload['failed']} failed, {payload['skipped']} skipped, "
          f"{payload['errors']} errors -> {out}")
    for nodeid in rec.failed_ids + rec.error_ids:
        print(f"  FAILED {nodeid}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
