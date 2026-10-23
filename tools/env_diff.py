"""Promotion check: compare a candidate (UAT) warehouse with the baseline (prod) warehouse.

    python tools/env_diff.py                                 # data/env/uat vs data/warehouse (pipeline/envs.py)
    python tools/env_diff.py --expect changes.csv            # differences the change ticket explains
    python tools/env_diff.py --base-warehouse A.duckdb --candidate-warehouse B.duckdb

For every model in the mart schemas (``dim_``, ``fct_``, ``mart_`` tables) it compares the row count and
the sum of every money column (amount, premium, cost, price, claims, fee, reserve, paid, incurred,
chargeback, commission, markup). A difference is ``explained`` when a row of the ``--expect`` CSV
(``table,measure,reason``; ``measure`` is ``rows``, a column name or ``*``) names it, otherwise it
``fail``s. The tool writes the latest result to ``data/env/env_diff.json`` (``ENV_DIFF_PATH``), which the
Operations page shows, and exits 1 on any unexplained difference (docs/operating-model/RELEASE.md).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.envs import ENVS, env_paths  # noqa: E402

MART_SCHEMAS = ("core", "dealer", "claims", "finance", "regulatory")
MODEL_PREFIX = re.compile(r"^(dim|fct|mart)_")
MONEY = re.compile(r"(amount|premium|cost|price|claims|fee|reserve|paid|incurred|chargeback|commission|markup)")
NOT_MONEY = re.compile(r"(rate|ratio|pct|percent|_days|_key$|frequency|severity|exposure)")
NUMERIC = ("DOUBLE", "FLOAT", "REAL", "DECIMAL", "NUMERIC")  # money is never an integer column
DEFAULT_OUT = Path(os.environ.get("ENV_DIFF_PATH", ROOT / "data" / "env" / "env_diff.json"))
AMOUNT_TOLERANCE = 0.01


def money_columns(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> list[str]:
    rows = con.execute(
        "select column_name, data_type from information_schema.columns where table_schema = ? and table_name = ? "
        "order by ordinal_position", [schema, table]).fetchall()
    return [c for c, t in rows if t.upper().startswith(NUMERIC) and MONEY.search(c) and not NOT_MONEY.search(c)]


def profile(path: Path) -> dict[str, dict[str, float]]:
    """{schema.table: {"rows": n, <money column>: sum, ...}} for every mart-layer model."""
    out: dict[str, dict[str, float]] = {}
    with duckdb.connect(str(path), read_only=True) as con:
        tables = con.execute(
            "select table_schema, table_name from information_schema.tables where table_schema in "
            f"({', '.join('?' * len(MART_SCHEMAS))}) order by 1, 2", list(MART_SCHEMAS)).fetchall()
        for schema, table in tables:
            if not MODEL_PREFIX.match(table):
                continue
            cols = money_columns(con, schema, table)
            exprs = ["count(*)", *[f'coalesce(sum("{c}"), 0)' for c in cols]]
            vals = con.execute(f'select {", ".join(exprs)} from "{schema}"."{table}"').fetchone()
            out[f"{schema}.{table}"] = {"rows": float(vals[0]),
                                        **{c: round(float(v), 2) for c, v in zip(cols, vals[1:], strict=True)}}
    return out


def read_expect(path: Path | None) -> dict[tuple[str, str], str]:
    if path is None:
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        return {(r["table"].strip(), r["measure"].strip()): r.get("reason", "").strip() for r in csv.DictReader(f)}


def diff(base: dict[str, dict[str, float]], cand: dict[str, dict[str, float]],
         expect: dict[tuple[str, str], str] | None = None, tolerance: float = AMOUNT_TOLERANCE) -> list[dict]:
    expect = expect or {}
    rows: list[dict] = []
    for table in sorted(set(base) | set(cand)):
        b, c = base.get(table), cand.get(table)
        measures = sorted(set(b or {}) | set(c or {}), key=lambda m: (m != "rows", m))
        for m in measures:
            bv, cv = (b or {}).get(m), (c or {}).get(m)
            if bv is None or cv is None:
                delta = None
                same = False
            else:
                delta = round(cv - bv, 2)
                same = abs(delta) < (0.5 if m == "rows" else tolerance)
            reason = expect.get((table, m)) or expect.get((table, "*"))
            status = "pass" if same else ("explained" if reason is not None else "fail")
            rows.append({"table": table, "measure": m, "base": bv, "candidate": cv, "diff": delta,
                         "status": status, "reason": reason or ""})
    return rows


def summarize(rows: list[dict], base_name: str, cand_name: str, base_path: Path, cand_path: Path) -> dict:
    counts = {s: sum(r["status"] == s for r in rows) for s in ("pass", "explained", "fail")}
    return {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "base": base_name,
            "candidate": cand_name, "base_warehouse": str(base_path), "candidate_warehouse": str(cand_path),
            "tables": len({r["table"] for r in rows}), "counts": counts,
            "status": "fail" if counts["fail"] else "pass", "differences": [r for r in rows if r["status"] != "pass"]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python tools/env_diff.py", description=__doc__.splitlines()[0])
    ap.add_argument("--base", choices=ENVS, default="prod")
    ap.add_argument("--candidate", choices=ENVS, default="uat")
    ap.add_argument("--base-warehouse", type=Path)
    ap.add_argument("--candidate-warehouse", type=Path)
    ap.add_argument("--expect", type=Path, help="CSV table,measure,reason of differences the change explains")
    ap.add_argument("--tolerance", type=float, default=AMOUNT_TOLERANCE, help="dollar tolerance per total")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    base_path = args.base_warehouse or env_paths(args.base).warehouse
    cand_path = args.candidate_warehouse or env_paths(args.candidate).warehouse
    for label, p in (("base", base_path), ("candidate", cand_path)):
        if not p.exists():
            print(f"env_diff: {label} warehouse not found: {p} (build it with python -m pipeline.run --env ...)")
            return 2
    rows = diff(profile(base_path), profile(cand_path), read_expect(args.expect), args.tolerance)
    result = summarize(rows, args.base, args.candidate, base_path, cand_path)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    c = result["counts"]
    print(f"env_diff {args.candidate} vs {args.base}: {result['tables']} tables, {c['pass']} pass, "
          f"{c['explained']} explained, {c['fail']} unexplained -> {args.out}")
    for r in result["differences"][:25]:
        print(f"  {r['status']:9} {r['table']}.{r['measure']}: {r['base']} -> {r['candidate']} {r['reason']}")
    return 1 if c["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
