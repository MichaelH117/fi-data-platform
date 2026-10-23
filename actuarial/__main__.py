"""CLI: ``python -m actuarial [--warehouse PATH] [--as-of YYYY-MM-DD] [--tail 1.0] [--strict-fit] [--write-docs]``.

Reads the ``core`` marts and (re)writes schema ``actuarial``:
  earned_premium              month x product cell x earning method (5 methods incl. the selected one)
  earning_method_assignment   which method / custom curve each product x new-used cell uses, and why
  earning_fit                 method cell / family x term decile: earned-premium curve vs actual loss
                              emergence on expired (or cancelled, truncated) contracts, A/E, KS gap, gate
  loss_triangle               segment x basis (reported / paid / count) x effective-quarter cohort x age
  development_factors         segment x basis x age: age-to-age factor, CDF and the tail (with its source)
  ibnr_by_cohort              segment x cohort: chain-ladder, Bornhuetter-Ferguson and selected IBNR;
                              ALL = sum of the segments (pooled-triangle ultimate kept as a diagnostic)
  accident_quarter            loss-quarter ultimate losses and loss ratios (selected + pro-rata earning);
                              IBNR from daily report lags (unreported claims + known-claim development)
  accident_triangle           the loss-quarter triangle behind it
  early_claim_emergence       cohort x product segment x window (30-180 days): early-claim rate
  early_claim_dealer          dealer x window (90, 180): actual vs expected early claims, flags
  loss_ratio_forecast         quarterly history + 4-quarter forecast per model, selected band
  forecast_backtest           rolling-origin MAE / RMSE / bias per model
  ibnr_backtest               the IBNR re-run at each of the 12 prior quarter-ends vs what emerged after
  valuation_view              segment x method to the valuation date: earned, incurred, loss ratio, UPR
  premium_deficiency          segment: selected UPR vs losses (+ expense) on unexpired coverage, PDR flag
  run_summary                 control totals (lines in vs triangle, unmatched claims, IBNR (also per
                              posting system), the factor-start quarter and age-1 factor, IBNR runoff
                              back-test, report lag, UPR, the valuation-date ITD figures and the
                              earning-fit gate: ``earning_fit_gate`` = warn when a credible row's curve
                              misses emergence by more than 10 points and more than sampling noise)

It also keeps the V1 names alive as views in ``gold_actuarial`` (earned_premium_by_curve,
loss_triangle, ibnr_by_cohort, development_factors) for one release, so the report shell keeps working.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from actuarial import build as b
from actuarial.data import load_inputs
from actuarial.docs import write_figures
from actuarial.emergence import early_claim_by_dealer, early_claim_emergence
from actuarial.forecast import forecast
from actuarial.methods import load_curves, load_rules

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
SCHEMA = "actuarial"
COMPAT_SCHEMA = "gold_actuarial"
log = logging.getLogger("actuarial")

COMPAT_VIEWS = {
    "earned_premium_by_curve": f"""
        select month, product_code, method as curve, sum(written_premium) as written_premium,
               sum(earned_premium) as earned_premium, sum(unearned_premium) as unearned_premium,
               sum(contracts_in_force) as contracts_in_force, sum(incurred_losses) as incurred_losses,
               sum(incurred_losses) / nullif(sum(earned_premium), 0) as loss_ratio
        from {SCHEMA}.earned_premium
        where method in ('pro_rata', 'rule_of_78s', 'reverse_rule_of_78s')
        group by all""",
    "loss_triangle": f"""
        select basis, cohort_label, cohort_start, age_quarters, incremental, cumulative
        from {SCHEMA}.loss_triangle where segment = 'ALL' and basis in ('reported', 'paid')""",
    "ibnr_by_cohort": f"select * exclude (segment) from {SCHEMA}.ibnr_by_cohort where segment = 'ALL'",
    "development_factors": f"""
        select age_quarters, ata, cdf from {SCHEMA}.development_factors
        where segment = 'ALL' and basis = 'reported'""",
}


class EarningFitError(RuntimeError):
    """Raised by ``run(strict_fit=True)`` when the earning-fit gate warns."""


def write_tables(con: duckdb.DuckDBPyConnection, tables: dict[str, pd.DataFrame]) -> None:
    con.execute("BEGIN")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    for name, df in tables.items():
        con.register("_df", df)
        con.execute(f"CREATE OR REPLACE TABLE {SCHEMA}.{name} AS SELECT * FROM _df")
        con.unregister("_df")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {COMPAT_SCHEMA}")
    v1_tables = {r[0] for r in con.execute(
        "select table_name from information_schema.tables where table_schema = ? and table_type = 'BASE TABLE'",
        [COMPAT_SCHEMA]).fetchall()}
    for name, sql in COMPAT_VIEWS.items():
        if name in v1_tables:  # V1 wrote tables here
            con.execute(f"DROP TABLE {COMPAT_SCHEMA}.{name}")
        con.execute(f"CREATE OR REPLACE VIEW {COMPAT_SCHEMA}.{name} AS {sql}")
    con.execute("COMMIT")


def _age1_link(acc: pd.DataFrame) -> float:
    """Accident-quarter age-1 -> age-2 development factor, from the CDFs (CDF_1 / CDF_2)."""
    cdf = acc.drop_duplicates("age_quarters").set_index("age_quarters")["cdf"]
    return float(cdf[1] / cdf[2]) if {1, 2} <= set(cdf.index) and cdf[2] else float("nan")


def compute(inputs, settings: b.Settings) -> dict[str, pd.DataFrame]:
    """Every output table from the inputs (pure; no warehouse writes)."""
    rules, curves = load_rules(), load_curves()
    val_q = b.valuation_quarter(settings.as_of)
    amounts = b.claim_amounts(inputs.claims, inputs.lines, settings.as_of)
    c = b.prepare_contracts(inputs.contracts, inputs.claims, rules, settings)

    ep = b.earned_premium(c, amounts, settings, curves)
    triangles = b.policy_triangles(c, inputs.claims, inputs.lines, val_q)
    fstart = b.factor_start(c, settings)
    acc, acc_tri = b.accident_quarters(inputs.claims, inputs.lines, ep, settings, fstart)
    occurred = b.occurred_ibnr(c, inputs.claims, inputs.lines, acc, val_q)
    ibnr, factors = b.ibnr_by_cohort(c, triangles, settings, occurred, fstart)
    by_source = b.ibnr_by_source(inputs.claims, inputs.lines, acc, val_q)
    _, lag_diag = b.report_lag_ibnr(inputs.claims, inputs.lines, val_q, fstart, settings.factor_min_cohorts)
    ibnr_bt = b.ibnr_backtest(inputs.claims, inputs.lines, settings, fstart)
    vv = b.valuation_view(c, ep, inputs.claims, inputs.lines, settings)
    pdr = b.premium_deficiency(vv, ibnr, settings)
    fit = b.earning_fit(c, amounts, settings, curves)

    hist = acc[acc["earned_premium"] >= 0.02 * acc["earned_premium"].max()]
    q_index = hist["quarter_start"].dt.year * 4 + (hist["quarter_start"].dt.month - 1) // 3
    fc, bt = forecast(pd.Series(hist["ultimate_loss_ratio"].to_numpy(), index=q_index.to_numpy()))
    fc.insert(0, "quarter", b._quarter_label(fc["q"]).to_numpy())
    fc.insert(1, "quarter_start", b._quarter_start(fc["q"]).to_numpy())
    fc = fc.drop(columns="q")

    val_end = pd.Timestamp(b.quarter_end(val_q))
    lines_to_val = inputs.lines[inputs.lines["posted_date"] <= val_end]
    ex = inputs.excluded_lines
    ex_to_val = ex[ex["posted_date"] <= val_end] if len(ex) else pd.DataFrame(columns=["incurred", "reason"])
    ex_claims = inputs.excluded_claims if len(inputs.excluded_claims) else pd.DataFrame(columns=["reason"])

    def excluded(reason: str) -> float:
        return float(ex_to_val.loc[ex_to_val["reason"] == reason, "incurred"].sum())
    tri_all = triangles[b.ALL]["reported"]
    all_ibnr = ibnr[ibnr["segment"] == b.ALL]
    ibnr_all = float(all_ibnr["selected_ibnr"].sum())
    vv_sel = vv[(vv["segment"] == b.ALL) & (vv["method"] == "selected")].iloc[0]
    pdr_all = pdr[pdr["segment"] == b.ALL].iloc[0]
    runoff = b.ibnr_runoff(inputs.claims, inputs.lines, settings, ibnr_all)
    seg_ib = ibnr[ibnr["segment"] != b.ALL]
    lag = b.report_lag_stats(inputs.claims, settings.as_of)
    summary = pd.DataFrame(
        [
            ("as_of", str(settings.as_of)),
            ("valuation_date", str(val_end.date())),
            ("contracts", len(c)),
            ("claims", len(inputs.claims)),
            ("payment_lines", len(inputs.lines)),
            # every line read from the claims fact; in scope (the triangle) + the two exclusions below
            ("incurred_lines_to_valuation", float(lines_to_val["incurred"].sum()) + float(ex_to_val["incurred"].sum())),
            ("incurred_lines_without_contract", excluded("no_contract") + float(lines_to_val.loc[
                ~lines_to_val["contract_id"].isin(c["contract_id"]), "incurred"].sum())),
            ("incurred_excluded_loss_before_sale", excluded("loss_before_sale")),
            ("claims_excluded_no_contract", int((ex_claims["reason"] == "no_contract").sum())),
            ("claims_excluded_loss_before_sale", int((ex_claims["reason"] == "loss_before_sale").sum())),
            ("incurred_in_policy_triangle", float(tri_all.ffill(axis=1).iloc[:, -1].sum())),
            ("ibnr_selected_all", float(all_ibnr["selected_ibnr"].sum())),
            ("reported_all", float(all_ibnr["reported_to_date"].sum())),
            ("unexpired_risk_losses_all", float(all_ibnr["unexpired_risk_losses"].sum())),
            ("selected_ultimate_all", float(all_ibnr["selected_ultimate"].sum())),
            ("selected_ultimate_sum_of_segments", float(seg_ib["selected_ultimate"].sum())),
            # diagnostic only: one pooled triangle with one ELR (ALL above is the sum of the segments)
            ("selected_ultimate_pooled_triangle", float(all_ibnr["all_triangle_ultimate"].sum())),
            ("ibnr_accident_quarter_basis", float(acc["ibnr"].sum())),
            ("factor_start_quarter", str(b._quarter_label(pd.Series([fstart])).iloc[0]) if fstart is not None
             else "all"),
            *((f"ibnr_source_{k}", v) for k, v in sorted(by_source.items())),
            ("accident_link_age1", _age1_link(acc)),
            ("ibnr_chain_ladder_accident_quarter", float(acc["chain_ladder_ibnr"].sum())),
            *lag_diag.items(),
            *b.ibnr_backtest_summary(ibnr_bt).items(),
            ("forecast_next_quarter", float(fc.loc[fc["kind"] == "forecast", "forecast"].iloc[0])),
            ("forecast_last_history", float(fc.loc[fc["kind"] == "history", "actual"].iloc[-1])),
            ("ibnr_selected_sum_of_segments", float(seg_ib["selected_ibnr"].sum())),
            ("valuation_period_key", int(vv_sel["valuation_period_key"])),
            ("earned_itd_selected_at_valuation", float(vv_sel["earned_premium"])),
            ("incurred_itd_at_valuation", float(vv_sel["incurred_losses"])),
            ("loss_ratio_itd_at_valuation", float(vv_sel["loss_ratio"])),
            ("upr_selected", float(pdr_all["unearned_premium"])),
            ("unexpired_to_upr_ratio", float(pdr_all["unexpired_to_upr"])),
            ("premium_deficiency_reserve_segments", float(pdr.loc[pdr["segment"] != b.ALL,
                                                                  "premium_deficiency_reserve"].sum())),
            *lag.items(),
            ("ibnr_days_of_incurred", b.ibnr_days_of_incurred(ibnr_all, acc, settings)),
            *runoff.items(),
            ("forecast_model", str(fc["selected_model"].iloc[0])),
            *b.earning_fit_summary(fit).items(),
        ],
        columns=["metric", "value"],
    ).astype({"value": str})

    return {
        "earned_premium": ep,
        "earning_method_assignment": b.method_assignment(c, rules),
        "earning_fit": fit,
        "loss_triangle": b.triangle_long(triangles),
        "development_factors": factors,
        "ibnr_by_cohort": ibnr,
        "accident_quarter": acc,
        "accident_triangle": acc_tri,
        "early_claim_emergence": early_claim_emergence(c, inputs.claims, settings.as_of),
        "early_claim_dealer": early_claim_by_dealer(c, inputs.claims, settings.as_of),
        "loss_ratio_forecast": fc,
        "forecast_backtest": bt,
        "ibnr_backtest": ibnr_bt,
        "valuation_view": vv,
        "premium_deficiency": pdr,
        "run_summary": summary,
    }


def run(
    warehouse: Path, as_of: date | None = None, tail: float = 1.0, strict_fit: bool = False,
    write_docs: bool = False,
) -> dict[str, int]:
    with duckdb.connect(str(warehouse)) as con:
        inputs = load_inputs(con)
        latest = max(inputs.contracts["effective_date"].max(), inputs.lines["posted_date"].max())
        settings = b.Settings(as_of=as_of or latest.date(), tail=tail)
        log.info("inputs: %d contracts, %d claims, %d payment lines, as of %s",
                 len(inputs.contracts), len(inputs.claims), len(inputs.lines), settings.as_of)
        tables = compute(inputs, settings)
        write_tables(con, tables)

    vv = tables["valuation_view"]
    for row in vv[vv["segment"] == b.ALL].itertuples():
        log.info(f"{row.method:<20} earned to valuation {row.earned_premium:>14,.0f}  loss ratio {row.loss_ratio:.1%}")
    s = tables["run_summary"].set_index("metric")["value"]
    log.info("IBNR %.0f (+ %.0f losses on unexpired coverage) on reported %.0f (valuation %s); forecast model %s",
             float(s["ibnr_selected_all"]), float(s["unexpired_risk_losses_all"]), float(s["reported_all"]),
             s["valuation_date"], s["forecast_model"])
    log.info("IBNR runoff: new claims %.0f in %.0f days after valuation (A/E %.0f%%, %.0f%% of the window's expected; "
             "%s); known-claim development %.0f; UPR %.0f, unexpired/UPR %.0f%%",
             float(s["ibnr_runoff_new_claims"]), float(s["ibnr_runoff_window_days"]),
             100 * float(s["ibnr_runoff_ratio"]), 100 * float(s["ibnr_runoff_ae_to_date"]), s["ibnr_runoff_flag"],
             float(s["case_development_known_claims"]),
             float(s["upr_selected"]), 100 * float(s["unexpired_to_upr_ratio"]))
    if s["ibnr_runoff_flag"] == "ibnr_short":
        log.info("IBNR runoff (information, not a gate): claims reported after valuation already exceed what the "
                 "%.0f-day window should bring (A/E to date %.2f)", float(s["ibnr_runoff_window_days"]),
                 float(s["ibnr_runoff_ae_to_date"]))
    log.info("IBNR back-test over %.0f earlier quarter-ends: mean absolute error %.0f on mean realized %.0f "
             "(%.0f%%); estimate / realized %.2f", float(s["ibnr_backtest_valuations"]), float(s["ibnr_backtest_mae"]),
             float(s["ibnr_backtest_mean_realized"]), 100 * float(s["ibnr_backtest_scaled_mae"]),
             float(s["ibnr_backtest_bias"]))
    if write_docs:
        nxt = tables["loss_ratio_forecast"]
        nxt = nxt[nxt["kind"] == "forecast"].iloc[0]
        ok = write_figures(s.to_dict(), {"lower": float(nxt["lower"]), "upper": float(nxt["upper"])})
        log.info("docs/ACTUARIAL.md figures %s", "updated" if ok else "not updated (markers missing)")
    if s["earning_fit_gate"] == "warn":
        log.warning("earning-fit gate WARN: %s of %s credible rows miss loss emergence by > %.0f points; "
                    "worst %s at %.0f points (see actuarial.earning_fit)",
                    s["earning_fit_rows_warn"], s["earning_fit_rows_credible"],
                    100 * float(s["earning_fit_gap_threshold"]), s["earning_fit_worst_row"],
                    100 * float(s["earning_fit_worst_gap"]))
        if strict_fit:
            raise EarningFitError(f"earning-fit gate failed: {s['earning_fit_worst_row']}")
    return {name: len(df) for name, df in tables.items()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m actuarial", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--as-of", type=date.fromisoformat, help="default: latest effective or posting date")
    ap.add_argument("--tail", type=float, default=1.0, help="tail factor beyond the oldest age")
    ap.add_argument("--strict-fit", action="store_true",
                    help="exit 1 when the earning-fit gate warns (default: log a warning and continue)")
    ap.add_argument("--write-docs", action="store_true",
                    help="rewrite the generated figures block in docs/ACTUARIAL.md from this run")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        counts = run(args.warehouse, args.as_of, args.tail, strict_fit=args.strict_fit, write_docs=args.write_docs)
    except EarningFitError as exc:
        log.error("%s", exc)
        return 1
    log.info("wrote %s", ", ".join(f"{SCHEMA}.{t} ({n} rows)" for t, n in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
