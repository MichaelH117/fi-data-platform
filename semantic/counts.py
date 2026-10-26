"""Semantic model vs warehouse headline counts (V7 edit 33): ``python -m semantic count-check``.

A few headline numbers are worked out twice:

* **model side**: the measure as written in the generated TMDL (``semantic/model/``), evaluated the way an
  import-mode refresh would see it: its ``SUM`` / ``COUNTROWS`` / ``DISTINCTCOUNT`` runs over the table and
  column the model loads (the partition source and ``sourceColumn``), with no filters and no RLS;
* **warehouse side**: the same number from the certified mart the report site and the finance pages show.

They must agree (counts exactly, money to the cent). A difference means the model binds to the wrong table
or column, or a mart and its fact have drifted, before anyone sees it in Power BI. The result also carries
when the model files were written and when the warehouse was built, for the Operations page.

Reads the TMDL files on disk and runs read-only queries; it writes nothing.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from semantic.export import Model, load_model

ROOT = Path(__file__).resolve().parents[1]
MODEL_DIR = ROOT / "semantic" / "model"
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
MONEY_TOLERANCE = 0.01
log = logging.getLogger("semantic.counts")


@dataclass(frozen=True)
class Headline:
    label: str  # plain words, as the Operations page shows it
    model: str  # domain model name (folder <model>.SemanticModel)
    measure: str  # measure name in that model's Key Measures table
    warehouse_sql: str  # the certified number
    warehouse_source: str  # where that number comes from, in words
    kind: str = "count"  # count | money


HEADLINES: tuple[Headline, ...] = (
    Headline("Contracts written", "Sales", "Contracts Written",
             "select sum(contracts_written) from finance.mart_pnl_by_product", "finance profit-and-loss mart"),
    Headline("Dealer cost written", "Sales", "Gross Coverage Cost",
             "select sum(written_coverage_cost) from finance.mart_pnl_by_product", "finance profit-and-loss mart",
             "money"),
    Headline("Claims reported", "Claims", "Claims Reported (loss month)",
             "select count(*) from core.dim_claim", "claim dimension (one row per claim)"),
    Headline("Claims paid", "Finance", "Claims Paid",
             "select sum(paid_claims) from finance.mart_pnl_by_product", "finance profit-and-loss mart", "money"),
    Headline("Cash remitted", "Finance", "Remitted Cash",
             "select sum(remitted_amount) from dealer.mart_remittance_aging", "dealer remittance-aging mart",
             "money"),
    Headline("Late fees", "Finance", "Late Fees",
             "select sum(late_fees) from dealer.mart_remittance_aging", "dealer remittance-aging mart", "money"),
)

_AGG = re.compile(r"^\s*(SUM|DISTINCTCOUNT)\(\s*'([^']+)'\[([^\]]+)\]\s*\)\s*$|^\s*COUNTROWS\(\s*'([^']+)'\s*\)\s*$")


@dataclass(frozen=True)
class Result:
    headline: Headline
    model_value: float | None
    warehouse_value: float | None
    model_sql: str
    note: str = ""

    @property
    def diff(self) -> float | None:
        if self.model_value is None or self.warehouse_value is None:
            return None
        return self.model_value - self.warehouse_value

    @property
    def ok(self) -> bool:
        d = self.diff
        if d is None:
            return False
        return abs(d) < (MONEY_TOLERANCE if self.headline.kind == "money" else 0.5)


def _ident(name: str) -> str:
    return ".".join(f'"{p}"' for p in name.split("."))


def model_sql(model: Model, measure: str) -> str:
    """SQL for a simple aggregate measure over the table and column the model loads. Raises ValueError."""
    node = next((m for _, m in model.measures() if m.name == measure), None)
    if node is None:
        raise ValueError(f"{model.name}: no measure {measure!r}")
    m = _AGG.match(node.value.replace("\n", " "))
    if not m:
        raise ValueError(f"{model.name}[{measure}] is not a plain SUM / COUNTROWS / DISTINCTCOUNT: {node.value!r}")
    func, table, column, count_table = m.groups()
    t = model.tables.get(table or count_table)
    if t is None or not t.source_table:
        raise ValueError(f"{model.name}: table {table or count_table!r} has no source table")
    if count_table:
        return f"select count(*) from {_ident(t.source_table)}"
    src = t.columns.get(column)
    if not src:
        raise ValueError(f"{model.name}: {table}[{column}] has no source column")
    agg = f"sum({_ident(src)})" if func == "SUM" else f"count(distinct {_ident(src)})"
    return f"select {agg} from {_ident(t.source_table)}"


def load_models(model_dir: Path = MODEL_DIR) -> dict[str, Model]:
    out = {}
    for folder in sorted(model_dir.glob("*.SemanticModel")):
        if (folder / "definition").is_dir():
            m = load_model("modern_domain", folder)
            out[m.name] = m
    return out


def _value(con: duckdb.DuckDBPyConnection, sql: str) -> float | None:
    row = con.execute(sql).fetchone()
    return None if row is None or row[0] is None else float(row[0])


def check(con: duckdb.DuckDBPyConnection, model_dir: Path = MODEL_DIR) -> list[Result]:
    models = load_models(model_dir)
    out = []
    for h in HEADLINES:
        sql, note, mv, wv = "", "", None, None
        try:
            sql = model_sql(models[h.model], h.measure) if h.model in models else ""
            if not sql:
                raise ValueError(f"model {h.model} not found in {model_dir}")
            mv = _value(con, sql)
        except (ValueError, duckdb.Error) as exc:
            note = str(exc).splitlines()[0][:160]
        try:
            wv = _value(con, h.warehouse_sql)
        except duckdb.Error as exc:
            note = note or str(exc).splitlines()[0][:160]
        out.append(Result(h, mv, wv, sql, note))
    return out


def model_written_at(model_dir: Path = MODEL_DIR) -> datetime | None:
    """When the model files were last generated (the manifest python -m semantic writes with them)."""
    path = model_dir / "manifest.json"
    return datetime.fromtimestamp(path.stat().st_mtime, UTC) if path.is_file() else None


def warehouse_built_at(con: duckdb.DuckDBPyConnection) -> datetime | None:
    try:
        row = con.execute("select max(_built_at) from finance.mart_pnl_by_product").fetchone()
    except duckdb.Error:
        return None
    if not row or row[0] is None:
        return None
    ts = row[0]
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m semantic count-check", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--model", type=Path, default=MODEL_DIR)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    with duckdb.connect(str(args.warehouse), read_only=True) as con:
        results = check(con, args.model)
    for r in results:
        log.log(logging.INFO if r.ok else logging.ERROR, "%-20s model %s  warehouse %s  diff %s %s",
                r.headline.label, r.model_value, r.warehouse_value, r.diff, r.note)
    bad = [r for r in results if not r.ok]
    log.info("%d of %d headline counts agree", len(results) - len(bad), len(results))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
