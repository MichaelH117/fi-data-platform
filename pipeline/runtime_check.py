"""Runtime regression check for the default pipeline (docs/V3_DESIGN.md 4.3 and 8.4).

``python -m pipeline.runtime_check [--runs DIR] [--max-regression 0.20] [--budget 300]``

Reads the run summaries ``python -m pipeline.run`` writes to ``data/runs/<run_id>.json`` and compares the
latest successful run with the previous successful run that executed the SAME steps (a run that skipped
generate is not compared with one that ran it). The opt-in ``postgres`` step is never counted: the default
pipeline does not need Postgres. Prints a per-step table. Exit code 1 when the latest run is more than
``--max-regression`` slower than the one before it; 0 otherwise (also when there is nothing to compare).
The 5-minute budget is reported, not enforced here: CI's e2e job enforces it with its step timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = Path(os.environ.get("PIPELINE_RUNS_DIR", ROOT / "data" / "runs"))
OPT_IN_STEPS = frozenset({"postgres"})
MAX_REGRESSION = 0.20
BUDGET_SECONDS = 300.0


@dataclass(frozen=True)
class Run:
    run_id: str
    steps: dict[str, float]  # executed default-pipeline steps -> seconds

    @property
    def seconds(self) -> float:
        return round(sum(self.steps.values()), 2)

    @property
    def shape(self) -> tuple[str, ...]:
        return tuple(sorted(self.steps))


def load_runs(runs_dir: Path = RUNS_DIR) -> list[Run]:
    """Successful runs, oldest first. Steps that were skipped or blocked are not 'executed'."""
    out = []
    for path in sorted(runs_dir.glob("*.json")):
        if path.name.startswith("_"):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if doc.get("status") != "ok":
            continue
        steps = {s["name"]: float(s.get("seconds") or 0.0) for s in doc.get("steps", [])
                 if s.get("status") == "ok" and s.get("name") not in OPT_IN_STEPS}
        if steps:
            out.append(Run(str(doc.get("run_id", path.stem)), steps))
    return sorted(out, key=lambda r: r.run_id)


def compare(runs: Sequence[Run]) -> tuple[Run | None, Run | None]:
    """(latest, the previous run with the same executed steps), either may be None."""
    if not runs:
        return None, None
    latest = runs[-1]
    prev = next((r for r in reversed(runs[:-1]) if r.shape == latest.shape), None)
    return latest, prev


def regression(latest: Run, prev: Run) -> float:
    return (latest.seconds - prev.seconds) / prev.seconds if prev.seconds else 0.0


def report(latest: Run, prev: Run | None, budget: float = BUDGET_SECONDS) -> str:
    lines = [f"latest run {latest.run_id}: {latest.seconds:.0f}s (budget {budget:.0f}s"
             f"{', over' if latest.seconds > budget else ''})"]
    if prev is not None:
        lines.append(f"previous comparable run {prev.run_id}: {prev.seconds:.0f}s "
                     f"({regression(latest, prev):+.0%})")
    lines.append(f"{'step':<12} {'latest':>8} {'previous':>9}")
    for name, secs in latest.steps.items():
        before = "" if prev is None else f"{prev.steps.get(name, 0.0):9.1f}"
        lines.append(f"{name:<12} {secs:8.1f} {before}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.runtime_check", description=__doc__.splitlines()[0])
    ap.add_argument("--runs", type=Path, default=RUNS_DIR)
    ap.add_argument("--max-regression", type=float, default=MAX_REGRESSION)
    ap.add_argument("--budget", type=float, default=BUDGET_SECONDS)
    args = ap.parse_args(argv)
    latest, prev = compare(load_runs(args.runs))
    if latest is None:
        print(f"no successful runs in {args.runs}; nothing to check")
        return 0
    print(report(latest, prev, args.budget))
    if prev is not None and regression(latest, prev) > args.max_regression:
        print(f"FAILED: the latest run is {regression(latest, prev):.0%} slower than the previous comparable run "
              f"(limit {args.max_regression:.0%})")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
