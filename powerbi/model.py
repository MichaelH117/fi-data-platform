"""The ``F&I Data Platform`` reporting semantic model: the four domain models in one, for one internal report.

A Power BI report binds to exactly ONE semantic model, and the five report pages need measures from all
four domain models (dealer scorecard: Sales; claims: Claims; funded reserve: Compliance; P&L: Finance).
So this builds one combined reporting model from the same plan as ``python -m semantic``: the same
measures (every one still maps to a ``metrics.yml`` metric), calculation group, relationships and RLS
rules, over the union of the domains' facts and every conformed dimension. Nothing is hand-written
except the actuarial IBNR table below, which is not a dbt model (see ``semantic.spec.HEADLINE_KPIS_OUTSIDE``).

Audience: internal. It carries all three roles so authors can test the dealer and partner views with
"View as". External roles never see the Compliance filing tables or IBNR: those get a ``FALSE()``
filter (``docs/ACCESS_MATRIX.md``: compliance data is internal only). External dealers keep using the
Sales / Claims / Finance domain models.
"""

from __future__ import annotations

import json
from pathlib import Path

from semantic import build as sb
from semantic import spec
from semantic.metrics import Catalog, friendly, q
from semantic.tmdl import TAB, annotation, description, join, name, prop_expr

MODEL_NAME = "F&I Data Platform"  # display name (database, theme, exports)
FILE_NAME = "FI Data Platform"  # file/folder name: "&" is unsafe in paths and URLs
DOMAIN = "Portfolio"
TARGET = "postgres"  # the local server filled by python -m warehouse.postgres sync
PURPOSE = ("Internal portfolio report model: Sales, Claims, Finance and Compliance measures in one model "
           "for the F&I Data Platform report (powerbi/). External audiences use the domain models.")
PBISM_SCHEMA = "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/definitionProperties/1.0.0/schema.json"
ROLES = ("Internal", "Tenant Partner", "Dealer User")

IBNR = "IBNR by Cohort"
IBNR_SOURCE = "gold_actuarial.ibnr_by_cohort"
# (column, TMDL type, hidden). One row per accident-quarter cohort at the valuation date.
IBNR_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("cohort_label", "string", False),
    ("cohort_start", "dateTime", False),
    ("age_quarters", "int64", False),
    ("contracts", "int64", True),
    ("written_premium", "double", True),
    ("ultimate_premium", "double", True),
    ("paid_to_date", "double", True),
    ("reported_to_date", "double", True),
    ("selected_ultimate", "double", True),
    ("selected_ibnr", "double", True),
    ("method", "string", False),
    ("valuation_date", "dateTime", False),
)
# Actuarial output measures (not dbt metrics): name -> (DAX, format, description).
IBNR_MEASURES: dict[str, tuple[str, str, str]] = {
    "IBNR (selected)": (f"SUM({q(IBNR)}[Selected IBNR])", "currency",
                        "Selected IBNR reserve (chain-ladder / Bornhuetter-Ferguson pick) at the valuation date."),
    "Ultimate Losses (selected)": (f"SUM({q(IBNR)}[Selected Ultimate])", "currency",
                                   "Selected ultimate losses: paid to date plus IBNR."),
    "Paid To Date (actuarial)": (f"SUM({q(IBNR)}[Paid To Date])", "currency",
                                 "Claims paid to date by accident-quarter cohort, as valued."),
    "Ultimate Loss Ratio (actuarial)": (f"DIVIDE(SUM({q(IBNR)}[Selected Ultimate]), SUM({q(IBNR)}[Ultimate Premium]))",
                                        "percent", "Selected ultimate losses over ultimate earned premium."),
}
# Internal-only tables: an external role's filter hides every row (facts or disconnected, so the filter
# never spreads to a shared dimension).
INTERNAL_ONLY = ("Funded Reserve Filing", "Regulatory Line Value", IBNR)


def portfolio_model(plan: sb.Plan, columns: sb.Columns) -> sb.DomainModel:
    metrics = sorted({m.metric for dm in plan.models.values() for m in dm.measures})
    return sb._domain_model(DOMAIN, metrics, plan.measures, columns, allowed=tuple(spec.DIMS), roles=ROLES,
                            purpose=PURPOSE)


def _ibnr_m() -> str:
    schema, table = IBNR_SOURCE.split(".")
    cols = ", ".join(f'"{c}"' for c, _, _ in IBNR_COLUMNS)
    return "\n".join([
        "let",
        "    Source = PostgreSQL.Database(PostgresServer, PostgresDatabase),",
        f'    Data = Source{{[Schema = "{schema}", Item = "{table}"]}}[Data],',
        f"    Kept = Table.SelectColumns(Data, {{{cols}}})",
        "in",
        "    Kept",
    ])


def render_ibnr() -> str:
    out = [*description("Actuarial IBNR by accident-quarter cohort (Python valuation output, not a dbt model). "
                        "Disconnected: no tenant or dealer key, so slicers do not filter it and external "
                        "roles cannot see it.", 0), f"table {name(IBNR)}"]
    for m, (dax, fmt, desc) in IBNR_MEASURES.items():
        out += ["", *description(desc, 1), *prop_expr(f"measure {name(m)}", dax, 1),
                f"{TAB * 2}formatString: {sb.FORMATS[fmt]}", f"{TAB * 2}displayFolder: Actuarial",
                annotation("source", "actuarial valuation (gold_actuarial.ibnr_by_cohort), not metrics.yml", 2)]
    for c, dtype, hidden in IBNR_COLUMNS:
        out += ["", f"{TAB}column {name(friendly(c))}", f"{TAB * 2}dataType: {dtype}"]
        if hidden:
            out.append(f"{TAB * 2}isHidden")
        if dtype in ("int64", "double"):
            out.append(f"{TAB * 2}summarizeBy: none")
        if dtype == "dateTime":
            out.append(f"{TAB * 2}formatString: yyyy-mm-dd")
        out.append(f"{TAB * 2}sourceColumn: {c}")
    out += ["", f"{TAB}partition {name(IBNR)} = m", f"{TAB * 2}mode: import", *prop_expr("source", _ibnr_m(), 2),
            "", annotation("source_table", IBNR_SOURCE, 1), annotation("table_kind", "actuarial", 1)]
    return join(out)


def write_portfolio(plan: sb.Plan, columns: sb.Columns, base: Path, put) -> sb.DomainModel:
    """Write ``<base>`` (a ``.SemanticModel`` folder) with ``put(path, text)``."""
    dm = portfolio_model(plan, columns)
    files: dict[Path, str] = {}
    sb.write_domain(dm, columns, base, lambda p, t: files.__setitem__(p, t), TARGET,
                    role_extra={t: "FALSE()" for t in INTERNAL_ONLY})
    defn = base / "definition"
    files[base / "definition.pbism"] = json.dumps(
        {"$schema": PBISM_SCHEMA, "version": "4.0", "settings": {}}, indent=2) + "\n"
    files[defn / "database.tmdl"] = join([f"database {name(MODEL_NAME)}", f"{TAB}compatibilityLevel: 1601"])
    model = files[defn / "model.tmdl"]
    files[defn / "model.tmdl"] = model.replace("\n\nref role ", f"\nref table {name(IBNR)}\n\nref role ", 1)
    files[defn / "tables" / f"{IBNR}.tmdl"] = render_ibnr()
    for p in sorted(files):
        put(p, files[p])
    return dm


def load_plan() -> tuple[sb.Plan, sb.Columns]:
    text = sb.METRICS_PATH.read_text(encoding="utf-8")
    columns = sb.load_lock()
    return sb.make_plan(Catalog.load(sb.METRICS_PATH), columns, text), columns
