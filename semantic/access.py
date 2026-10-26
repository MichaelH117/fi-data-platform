"""App audiences: which report each audience of each app sees (``docs/ACCESS_MATRIX.md`` section 8).

An app audience only decides which reports show up in the app's navigation. It is not security: the
rows a reader sees come from the model's RLS role, so a hidden report never needs to be "safe" and a
visible one is still filtered. No report is ever shared directly (links, direct access): app only.

``python -m semantic access-audit [--out FILE]`` writes the audience x report list as CSV and exits 1
when a rule below is broken. ``tests/test_semantic.py`` checks the doc table is ``markdown()``.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from dataclasses import dataclass
from pathlib import Path

from semantic import spec

VISIBLE, HIDDEN, NOT_IN_APP = "visible", "hidden", "-"
SHARED_DIRECTLY = "never"


@dataclass(frozen=True)
class Audience:
    name: str
    rls_role: str  # the RLS role its security groups map to (ACCESS_MATRIX section 4)
    external: bool = False


AUDIENCES: tuple[Audience, ...] = (
    Audience("Executive", "Internal"),
    Audience("Finance", "Internal"),
    Audience("Claims operations", "Internal"),
    Audience("Compliance", "Internal"),
    Audience("Field team", "Internal"),
    Audience("External partner", "Tenant Partner", external=True),
    Audience("Dealer", "Dealer User", external=True),
)
AUDIENCE = {a.name: a for a in AUDIENCES}

# App -> its audiences (each audience is one or more security groups, ACCESS_MATRIX section 5).
APPS: dict[str, tuple[str, ...]] = {
    "VSC Internal": ("Executive", "Finance", "Claims operations", "Field team"),
    "VSC Compliance": ("Executive", "Compliance"),
    "Dealer & Partner Reports": ("External partner", "Dealer"),
}
EXTERNAL_APPS = ("Dealer & Partner Reports",)  # the only apps an external audience may belong to


@dataclass(frozen=True)
class Report:
    app: str
    name: str
    models: tuple[str, ...]  # semantic models it reads (spec.DOMAIN_ROLES keys)
    visible_to: tuple[str, ...]  # audiences of the app that see it; the rest of the app's audiences: hidden
    note: str = ""


REPORTS: tuple[Report, ...] = (
    Report("VSC Internal", "Executive overview", ("Sales", "Claims", "Finance"),
           ("Executive", "Finance", "Claims operations", "Field team")),
    Report("VSC Internal", "Dealer scorecard", ("Sales", "Claims"),
           ("Executive", "Finance", "Claims operations", "Field team")),
    Report("VSC Internal", "Claims & IBNR", ("Claims",), ("Executive", "Finance", "Claims operations"),
           "IBNR is actuarial output"),
    Report("VSC Internal", "Finance", ("Finance",), ("Executive", "Finance")),
    Report("VSC Internal", "Experience", ("Finance", "Sales", "Claims"),
           ("Executive", "Finance", "Claims operations"), "V6 edit 1"),
    Report("VSC Internal", "New Reserves", ("Finance", "Sales"), ("Executive", "Finance"), "V6 edit 11"),
    Report("VSC Internal", "Commission hierarchy", ("Finance",), ("Executive", "Finance"),
           "V6 edit 12; agent pay is compensation data"),
    Report("VSC Compliance", "Regulatory funded reserve", ("Compliance",), ("Executive", "Compliance")),
    Report("VSC Compliance", "State in-force contract lists", ("Compliance",), ("Compliance",),
           "contract-level rows"),
    Report("Dealer & Partner Reports", "Dealer scorecard (partner view)", ("Sales", "Claims"),
           ("External partner", "Dealer")),
    Report("Dealer & Partner Reports", "Claims (partner view)", ("Claims",), ("External partner", "Dealer")),
    Report("Dealer & Partner Reports", "Partner settlement statement", ("Finance",), ("External partner",),
           "V6 edit 17; one statement per partner"),
)


def visibility(report: Report, audience: str) -> str:
    if audience not in APPS[report.app]:
        return NOT_IN_APP
    return VISIBLE if audience in report.visible_to else HIDDEN


def audit() -> list[str]:
    """Rule breaks (empty = clean)."""
    problems: list[str] = []
    for app, members in APPS.items():
        problems += [f"{app}: unknown audience {a!r}" for a in members if a not in AUDIENCE]
    for r in REPORTS:
        where = f"{r.app} / {r.name}"
        if r.app not in APPS:
            problems.append(f"{where}: unknown app")
            continue
        if not r.visible_to:
            problems.append(f"{where}: no audience sees it")
        problems += [f"{where}: {a!r} is not an audience of the app" for a in r.visible_to if a not in APPS[r.app]]
        problems += [f"{where}: unknown model {m!r}" for m in r.models if m not in spec.DOMAIN_ROLES]
        for a in r.visible_to:
            aud = AUDIENCE.get(a)
            if not aud:
                continue
            # A visible report must be readable under the audience's RLS role in every model it uses, so
            # an external audience can never see a Compliance (internal-only) report.
            missing = [m for m in r.models if aud.rls_role not in spec.DOMAIN_ROLES.get(m, ())]
            if missing:
                problems.append(f"{where}: {a} ({aud.rls_role}) has no role in {', '.join(missing)}")
    for a in AUDIENCES:
        internal_apps = [app for app, members in APPS.items() if a.name in members and app not in EXTERNAL_APPS]
        if a.external and internal_apps:
            problems.append(f"{a.name}: external audience in internal app(s) {', '.join(internal_apps)}")
    return problems


def rows() -> list[dict[str, str]]:
    """One row per app x audience x report (the CSV)."""
    out = []
    for r in REPORTS:
        for a in APPS[r.app]:
            out.append({"app": r.app, "audience": a, "report": r.name, "models": " + ".join(r.models),
                        "rls_role": AUDIENCE[a].rls_role, "visibility": visibility(r, a),
                        "shared_directly": SHARED_DIRECTLY})
    return out


def to_csv() -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows()[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows())
    return buf.getvalue()


def markdown() -> str:
    """The section-8 table of docs/ACCESS_MATRIX.md (one row per report, one column per audience)."""
    head = ["App", "Report", "Models", *[a.name for a in AUDIENCES], "Shared directly"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in REPORTS:
        cells = [r.app, r.name + (f" ({r.note})" if r.note else ""), ", ".join(r.models),
                 *[visibility(r, a.name) for a in AUDIENCES], SHARED_DIRECTLY]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m semantic access-audit", description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, help="CSV file to write (default: stdout)")
    args = ap.parse_args(argv)
    text = to_csv()
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(text)
    problems = audit()
    for p in problems:
        print(f"access-audit: {p}", file=sys.stderr)
    return 1 if problems else 0
