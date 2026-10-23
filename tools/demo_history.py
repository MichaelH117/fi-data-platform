"""Demo run history: ``python tools/demo_history.py [--days N] [--fail-day K] [--no-build] [--dry-run]``.

The Operations page (``ops.html``) reads ``audit.pipeline_run``. After one build it holds one run,
so the run history, the gates and the recovery path are invisible. This routine fills it with real runs,
using only the public CLIs (``python -m pipeline.run`` and ``python -m generator``):

1. **build**: ``pipeline.run`` (generates the sources on a first run, else runs every step).
2. **daily runs**: ``pipeline.run --append-day`` once per demo day.
3. **one injected gate failure** (day ``--fail-day``), then its recovery, the way an incident runs:

   a. ``pipeline.run --append-day --only generate feed ingest``: the day's extract lands in raw.
   b. ``python -m generator --append-day 1``: the source keeps writing *after* the extract cutoff
      (a late batch). Raw no longer ties to the source.
   c. ``pipeline.run --from dbt``: the reconciliation gate finds the break (source != raw) and exits 1.
      ``publish`` and ``reports`` are blocked; the run is recorded as ``failed``.
   d. ``pipeline.run --from feed``: the recovery run re-extracts, the gates pass, the site publishes.

Every run is a real run with a real ``audit.pipeline_run`` row. Nothing is back-dated: the 7 AM SLO
table counts business days by when runs actually finished, so a single sitting adds one tracked day.
Day-over-day SLO history comes from running ``day`` every business morning (``tasks.ps1 schedule``).

Each run takes a few minutes; ``--dry-run`` prints the plan without running anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # run as a script: make the repo packages importable
    sys.path.insert(0, str(ROOT))


@dataclass(frozen=True)
class Step:
    label: str
    argv: tuple[str, ...]  # arguments after the python executable
    expect_ok: bool = True  # False: the step must fail (the injected gate failure)
    fail_step: str = ""  # with expect_ok False: the pipeline step that must be the one that failed

    @property
    def is_pipeline_run(self) -> bool:
        return self.argv[:2] == ("-m", "pipeline.run")


def _run(*args: str) -> tuple[str, ...]:
    return ("-m", "pipeline.run", *args)


def plan(days: int = 3, fail_day: int = 2, build: bool = True) -> list[Step]:
    """The ordered demo steps. ``fail_day`` 0 means no injected failure."""
    if days < 0:
        raise ValueError("days must be >= 0")
    if fail_day and not 1 <= fail_day <= days:
        raise ValueError(f"fail_day must be between 1 and days ({days}), or 0 for none")
    steps = [Step("build: full pipeline run", _run())] if build else []
    for d in range(1, days + 1):
        if d != fail_day:
            steps.append(Step(f"day {d}: daily run", _run("--append-day")))
            continue
        steps += [
            Step(f"day {d}: extract the day into raw", _run("--append-day", "--only", "generate", "feed", "ingest")),
            Step(f"day {d}: INJECTED late source batch after the extract cutoff",
                 ("-m", "generator", "--append-day", "1")),
            Step(f"day {d}: transform + gates (reconciliation must fail, publish blocked)",
                 _run("--from", "dbt"), expect_ok=False, fail_step="reconcile"),
            Step(f"day {d}: recovery run (re-extract, gates pass, publish)", _run("--from", "feed")),
        ]
    return steps


Runner = Callable[[Sequence[str]], int]


def subprocess_runner(argv: Sequence[str]) -> int:
    return subprocess.run([sys.executable, *argv], cwd=ROOT, env=dict(os.environ), check=False).returncode


def last_failed_step(runs_dir: Path | None = None) -> str | None:
    """The failed step of the newest run summary in data/runs (``<run_id>.json``), or None."""
    if runs_dir is None:
        from pipeline.run import RUNS_DIR as runs_dir
    try:
        newest = max(Path(runs_dir).glob("2*.json"))
        steps = json.loads(newest.read_text(encoding="utf-8")).get("steps", [])
    except (OSError, ValueError):
        return None
    return next((s.get("name") for s in steps if s.get("status") == "failed"), None)


def execute(steps: Sequence[Step], runner: Runner = subprocess_runner, out=print,
            failed_step: Callable[[], str | None] = last_failed_step) -> int:
    """Run the steps in order. Stops (exit 1) when a step's outcome is not the expected one, including
    an expected failure that happened in the wrong step (a real dbt error must not pass as the demo)."""
    for i, s in enumerate(steps, 1):
        out(f"[demo {i}/{len(steps)}] {s.label}: python {' '.join(s.argv)}")
        t0 = time.perf_counter()
        code = runner(s.argv)
        ok = code == 0
        out(f"[demo {i}/{len(steps)}] exit {code} in {time.perf_counter() - t0:.0f}s"
            + ("" if s.expect_ok else " (expected failure)" if not ok else ""))
        if ok != s.expect_ok:
            want = "succeed" if s.expect_ok else "fail"
            out(f"[demo] stopped: '{s.label}' was expected to {want} (exit {code}). See data/runs/*.log.")
            return 1
        if not ok and s.fail_step and (got := failed_step()) != s.fail_step:
            out(f"[demo] stopped: expected step '{s.fail_step}' to fail, but the failed step was {got!r}.")
            return 1
    out("[demo] done: open site/ops.html for the run history (failed run, recovery, SLO).")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/demo_history.py", description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=3, help="demo business days to append (default 3)")
    ap.add_argument("--fail-day", type=int, default=2, help="day with the injected gate failure (0 = none)")
    ap.add_argument("--no-build", action="store_true", help="skip the initial full pipeline run")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    args = ap.parse_args(argv)
    try:
        steps = plan(args.days, args.fail_day, build=not args.no_build)
    except ValueError as exc:
        ap.error(str(exc))
    if args.dry_run:
        for s in steps:
            print(f"{s.label}: python {' '.join(s.argv)}" + ("" if s.expect_ok else "   # expected to fail"))
        return 0
    return execute(steps)


if __name__ == "__main__":
    raise SystemExit(main())
