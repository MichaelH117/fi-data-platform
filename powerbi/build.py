"""Assemble the Power BI project files (report + reporting model) and write or check them."""

from __future__ import annotations

from pathlib import Path

from powerbi import model, report

HERE = Path(__file__).resolve().parent
NAME = model.MODEL_NAME
PBIP = HERE / f"{NAME}.pbip"
REPORT_DIR = HERE / f"{NAME}.Report"
MODEL_DIR = HERE / f"{NAME}.SemanticModel"
GENERATED_DIRS = (REPORT_DIR, MODEL_DIR)
LOCAL = ".pbi"  # Desktop's per-user folder (localSettings.json, cache.abf): git-ignored, never touched


def build_files() -> dict[Path, str]:
    """Every generated file -> its text."""
    files: dict[Path, str] = {PBIP: report.dumps(report.pbip(REPORT_DIR.name))}
    for rel, doc in report.report_files(MODEL_DIR.name).items():
        files[REPORT_DIR / rel] = report.dumps(doc)
    plan, columns = model.load_plan()
    model.write_portfolio(plan, columns, MODEL_DIR, lambda p, t: files.__setitem__(p, t))
    return files


def _existing() -> set[Path]:
    out: set[Path] = set()
    for d in GENERATED_DIRS:
        if d.exists():
            out |= {p for p in d.rglob("*") if p.is_file() and LOCAL not in p.relative_to(d).parts}
    if PBIP.exists():
        out.add(PBIP)
    return out


def check(files: dict[Path, str]) -> list[str]:
    have = _existing()
    diffs = [f"missing: {p.relative_to(HERE)}" for p in sorted(set(files) - have)]
    diffs += [f"extra: {p.relative_to(HERE)}" for p in sorted(have - set(files))]
    diffs += [f"changed: {p.relative_to(HERE)}" for p in sorted(set(files) & have)
              if p.read_text(encoding="utf-8") != files[p]]
    return diffs


def write(files: dict[Path, str]) -> int:
    """Write changed files, delete stale generated ones (never ``.pbi/``). Returns files changed."""
    changed = 0
    for p in sorted(_existing() - set(files)):
        p.unlink()
        changed += 1
    for p, text in files.items():
        if p.exists() and p.read_text(encoding="utf-8") == text:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        changed += 1
    for d in GENERATED_DIRS:  # drop folders left empty by a removed page or visual
        for sub in sorted((x for x in d.rglob("*") if x.is_dir()), key=lambda x: len(x.parts), reverse=True):
            if not any(sub.iterdir()):
                sub.rmdir()
    return changed
