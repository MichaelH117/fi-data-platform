"""Everything the report site reads, in one place.

Metrics come from the gold marts. Where a page needs a grain the marts do not carry (portfolio
totals, dealer x tier), additive mart/fact columns are summed and the ratio is taken with the same
governed formula from docs/METRICS.md: never a ratio of ratios, never a different definition.
Peer numbers on dealer pages come only from the suppressed publish layer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

TERM_BAND_SQL = "case when term_months <= 36 then '24-36' when term_months <= 48 then '48' else '60-72' end"


def ratio(num: pd.Series | float, den: pd.Series | float):
    """num / den, null where the denominator is not positive."""
    if isinstance(den, pd.Series):
        return (num / den).where(den > 0)
    return num / den if den and den > 0 else None


@dataclass
class ReportData:
    as_of: date
    last_month: date  # last complete month; KPIs and trends stop here
    portfolio: pd.DataFrame  # month: written, earned, incurred, CIF, T12 sums and ratios
    dealer_monthly: pd.DataFrame  # dealer_code x month
    dealers: pd.DataFrame  # current dealer attributes + latest KPIs
    products: pd.DataFrame  # mart_product_performance
    cancel_cohorts: pd.DataFrame  # sale quarter: sold, cancelled
    claims_trend: pd.DataFrame  # mart_claims_trend
    ibnr: pd.DataFrame
    triangle: pd.DataFrame
    factors: pd.DataFrame
    curves: pd.DataFrame
    bench_t12: pd.DataFrame
    bench_monthly: pd.DataFrame
    manifest: dict
    quality: dict = field(default_factory=dict)
    # Posting period x book totals from finance.mart_pnl_by_product (see load_pnl_monthly). When set, the
    # executive overview's premium, loss-ratio and combined-ratio figures come from here, so they match the
    # Finance pages to the cent.
    pnl_monthly: pd.DataFrame | None = None
    # Dealer watch (owner: dealer & claims): dealer.mart_dealer_scorecard rows for last_month and the seed
    # rules (dealer_claim_kpi_rules scorecard_*). The overview and Dealers page flag dealers from these only,
    # so their counts equal the dealer scorecard page. See reports/dealer/watch.py.
    dealer_watch: pd.DataFrame | None = None
    dealer_watch_rules: dict = field(default_factory=dict)


def _df(con: duckdb.DuckDBPyConnection, sql: str, *params: object) -> pd.DataFrame:
    return con.execute(sql, list(params)).df()


def load_portfolio(con: duckdb.DuckDBPyConnection, last_month: date) -> pd.DataFrame:
    p = _df(
        con,
        """
        select month_start as month,
               sum(contracts_sold) as contracts_sold, sum(contracts_in_force) as contracts_in_force,
               sum(written_premium) as written_premium, sum(earned_premium) as earned_premium,
               sum(incurred_claims) as incurred_claims, sum(paid_claims) as paid_claims,
               sum(t12_earned_premium) as t12_earned_premium, sum(t12_incurred_claims) as t12_incurred_claims,
               sum(t12_claim_count) as t12_claim_count, sum(t12_exposure_years) as t12_exposure_years
        from gold.mart_dealer_loss_ratio
        where month_start <= ?
        group by 1 order by 1
        """,
        last_month,
    )
    # Expense ratio, statutory convention: acquisition + admin expense over written premium (T12).
    exp = _df(
        con,
        """
        select date_trunc('month', sale_date) as month,
               sum(admin_fee + commission) as expenses, sum(reserve_amount) as written
        from gold.fct_contract_sale group by 1
        """,
    )
    p = p.merge(exp, on="month", how="left").fillna({"expenses": 0.0, "written": 0.0})
    p["t12_expenses"] = p["expenses"].rolling(12, min_periods=12).sum()
    p["t12_written"] = p["written"].rolling(12, min_periods=12).sum()
    p["t12_loss_ratio"] = ratio(p["t12_incurred_claims"], p["t12_earned_premium"])
    p["t12_expense_ratio"] = ratio(p["t12_expenses"], p["t12_written"])
    p["t12_combined_ratio"] = p["t12_loss_ratio"] + p["t12_expense_ratio"]
    p["t12_claim_frequency"] = ratio(p["t12_claim_count"], p["t12_exposure_years"])
    p["month"] = pd.to_datetime(p["month"])
    return p


def pnl_closed_period(con: duckdb.DuckDBPyConnection) -> int | None:
    """Latest closed posting period, by the same rule as reports/finance (falls back to the latest period)."""
    q = "select max(posting_period_key) from finance.mart_cession_experience"
    closed = con.execute(q + " where is_closed").fetchone()[0]
    return closed if closed is not None else con.execute(q).fetchone()[0]


def load_pnl_monthly(con: duckdb.DuckDBPyConnection) -> pd.DataFrame | None:
    """Book totals per posting period up to the closed period, from the V3 P&L mart, plus T12 sums.

    Vocabulary (one meaning per label, the same on every page):
    * earned_premium: earned gross, all cost-split buckets (Finance "Earned premium").
    * earned_claims_reserve: the earned CLAIMS_RESERVE bucket, the loss-ratio denominator.
    * incurred_claims: posted incurred from fct_claim_payment_line.
    Ratios follow mart_pnl_by_product: loss = incurred / earned claims reserve; combined =
    (incurred + acquisition + admin fee) / earned premium.
    """
    try:
        closed = pnl_closed_period(con)
    except duckdb.Error:
        return None
    if closed is None:
        return None
    p = _df(
        con,
        """
        select posting_period_key,
               sum(written_coverage_cost) as written_coverage_cost, sum(earned_premium) as earned_premium,
               sum(earned_claims_reserve) as earned_claims_reserve, sum(incurred_claims) as incurred_claims,
               sum(earned_acquisition_cost) as earned_acquisition_cost, sum(earned_admin_fee) as earned_admin_fee
        from finance.mart_pnl_by_product
        where posting_period_key <= ?
        group by 1 order by 1
        """,
        closed,
    )
    p["month"] = pd.to_datetime(p["posting_period_key"].astype(int).astype(str) + "01", format="%Y%m%d")
    # Fill missing periods so a 12-row window is always 12 calendar months.
    full = pd.DataFrame({"month": pd.date_range(p["month"].min(), p["month"].max(), freq="MS")})
    p = full.merge(p.drop(columns="posting_period_key"), on="month", how="left").fillna(0.0)
    sums = ["written_coverage_cost", "earned_premium", "earned_claims_reserve", "incurred_claims",
            "earned_acquisition_cost", "earned_admin_fee"]
    for c in sums:
        p[f"t12_{c}"] = p[c].rolling(12, min_periods=12).sum()
    p["t12_loss_ratio"] = ratio(p["t12_incurred_claims"], p["t12_earned_claims_reserve"])
    p["t12_combined_ratio"] = ratio(
        p["t12_incurred_claims"] + p["t12_earned_acquisition_cost"] + p["t12_earned_admin_fee"],
        p["t12_earned_premium"])
    return p


def load_dealer_monthly(con: duckdb.DuckDBPyConnection, last_month: date) -> pd.DataFrame:
    # Summed over SCD2 versions of a dealer (one dealer_code can span several dealer_keys).
    d = _df(
        con,
        """
        select dealer_code, month_start as month,
               sum(contracts_sold) as contracts_sold, sum(contracts_in_force) as contracts_in_force,
               sum(written_premium) as written_premium, sum(earned_premium) as earned_premium,
               sum(incurred_claims) as incurred_claims,
               sum(t12_earned_premium) as t12_earned_premium, sum(t12_incurred_claims) as t12_incurred_claims,
               sum(t12_claim_count) as t12_claim_count, sum(t12_exposure_years) as t12_exposure_years
        from gold.mart_dealer_loss_ratio
        where not is_unknown_dealer and month_start <= ?
        group by 1, 2 order by 1, 2
        """,
        last_month,
    )
    d = d.sort_values(["dealer_code", "month"])
    g = d.groupby("dealer_code")
    d["itd_earned"] = g["earned_premium"].cumsum()
    d["itd_incurred"] = g["incurred_claims"].cumsum()
    d["t12_loss_ratio"] = ratio(d["t12_incurred_claims"], d["t12_earned_premium"])
    d["itd_loss_ratio"] = ratio(d["itd_incurred"], d["itd_earned"])
    d["t12_claim_frequency"] = ratio(d["t12_claim_count"], d["t12_exposure_years"])
    d["month"] = pd.to_datetime(d["month"])
    return d


def load_dealers(con: duckdb.DuckDBPyConnection, monthly: pd.DataFrame, t12_start: date,
                 last_month: date) -> pd.DataFrame:
    attrs = _df(
        con,
        """
        select dealer_code, dealer_name, city, state, dealer_status, participation_label, onboarded_date
        from gold.dim_dealer where is_current and dealer_key <> '-1'
        """,
    )
    latest = monthly[monthly["month"] == pd.Timestamp(last_month)].drop(columns="month")
    sev = _df(
        con,
        """
        select dealer_code, count(*) as paid_claims_n, sum(paid_amount) as paid_amount_t12
        from gold.fct_claim
        where claim_status = 'paid' and loss_date >= ? and loss_date < ? + interval 1 month
        group by 1
        """,
        t12_start,
        last_month,
    )
    out = attrs.merge(latest, on="dealer_code", how="left").merge(sev, on="dealer_code", how="left")
    out["t12_claim_severity"] = ratio(out["paid_amount_t12"], out["paid_claims_n"])
    return out.sort_values("dealer_code").reset_index(drop=True)


LAYERS = ("raw", "staging", "intermediate", "snapshots", "marts")


def _latest_layer_counts(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Rows per relation from the newest audit.layer_row_count run (written by the diagnostics step
    from the dbt manifest), so this page never queries a raw, staging or intermediate table."""
    try:
        df = _df(
            con,
            """
            select layer, schema_name, relation_name as table_name, materialized, row_count as rows
            from audit.layer_row_count
            where run_id = (select run_id from audit.layer_row_count order by counted_at desc limit 1)
            """,
        )
        df = df.assign(_o=df["layer"].map({k: i for i, k in enumerate(LAYERS)}))
        return df.sort_values(["_o", "schema_name", "table_name"]).drop(columns="_o").reset_index(drop=True)
    except duckdb.Error:
        return pd.DataFrame(columns=["layer", "schema_name", "table_name", "materialized", "rows"])


def load_quality(con: duckdb.DuckDBPyConnection, root: Path) -> dict:
    tables = _latest_layer_counts(con)
    by_layer = (tables.groupby("layer", sort=False).agg(relations=("table_name", "size"), rows=("rows", "sum"))
                .reset_index())
    ingest = _df(
        con,
        """
        select source_system, table_name, watermark_to, rows_read, rows_loaded, rows_quarantined, status, finished_at
        from audit.ingest_run
        qualify row_number() over (partition by source_system, table_name order by finished_at desc) = 1
        order by source_system, table_name
        """,
    )
    # Intake controls: what was rejected, why, a sample of the reject keys, and whether each row was
    # replayed, dropped as a duplicate, recovered, or is still open (core.mart_intake_quarantine).
    quarantine = _df(
        con,
        """
        select
            source_system, source_table, reject_reason,
            count(*) as rows,
            count(*) filter (where resolution_status = 'replayed') as replayed,
            count(*) filter (where resolution_status = 'duplicate_dropped') as duplicate_dropped,
            count(*) filter (where resolution_status = 'recovered') as recovered,
            count(*) filter (where resolution_status = 'open') as open,
            string_agg(distinct reject_key, ', ' order by reject_key) filter (where reject_key is not null)
                as reject_keys
        from core.mart_intake_quarantine
        group by all
        order by source_system, source_table, rows desc
        """,
    )
    quarantine["sample_keys"] = [", ".join((k or "").split(", ")[:3]) for k in quarantine.pop("reject_keys")]
    flags = con.execute(
        """
        select
          (select count(*) from core.dim_contract_vehicle where not is_valid_vin),
          (select count(*) from core.dim_contract where is_dealer_code_missing),
          (select count(*) from core.dim_contract where is_recovered_from_quarantine),
          (select count(*) from core.dim_claim where is_amount_sign_fixed),
          (select count(*) from core.dim_claim where not has_contract_key)
        """
    ).fetchone()
    flag_rows = [
        ("Contracts with an invalid VIN (kept, flagged)", flags[0]),
        ("Contracts with no dealer code (reported as UNKNOWN dealer)", flags[1]),
        ("Contracts recovered from quarantine (MM/DD/YYYY dates)", flags[2]),
        ("Claims with a negative amount (sign fixed)", flags[3]),
        ("Claims with no resolvable contract (kept, contract '-1')", flags[4]),
    ]
    runs_dir = Path(os.environ.get("PIPELINE_RUNS_DIR", root / "data" / "runs"))  # same override as pipeline.run
    return {
        "tables": tables,
        "layers": by_layer,
        "ingest": ingest,
        "quarantine": quarantine,
        "flags": flag_rows,
        "dbt": _dbt_results(root / "dbt" / "target", runs_dir),
        "last_run": _last_run(runs_dir),
    }


def _build_results(target: Path, runs: Path | None) -> dict | None:
    """The newest full dbt build results. pipeline.run saves the build's run_results.json to
    data/runs/<run_id>/dbt_build.json, because the gate steps' ``dbt test`` overwrites the target
    copy; a target file is used only when it is itself a build (it has model results) and newer."""
    found = []
    saved = sorted(runs.glob("*/dbt_build.json")) if runs is not None and runs.exists() else []
    for path in [*saved[-1:], target / "run_results.json"]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if any(r["unique_id"].startswith("model.") for r in data.get("results", [])):
            found.append(data)
    return max(found, key=lambda d: d["metadata"]["generated_at"]) if found else None


def _dbt_results(target: Path, runs: Path | None = None) -> dict:
    out: dict = {"available": False}
    data = _build_results(target, runs)
    if data is not None:
        results = data["results"]
        counts: dict[str, dict[str, int]] = {}
        for r in results:
            kind = r["unique_id"].split(".")[0]
            counts.setdefault(kind, {}).setdefault(r["status"], 0)
            counts[kind][r["status"]] += 1
        out = {
            "available": True,
            "generated_at": data["metadata"]["generated_at"][:19].replace("T", " ") + " UTC",
            "elapsed": round(data.get("elapsed_time", 0.0), 1),
            "counts": counts,
            "problems": [
                {"id": r["unique_id"], "status": r["status"], "message": r.get("message") or ""}
                for r in results
                if r["status"] not in ("success", "pass")
            ],
        }
    sf = target / "sources.json"
    if sf.exists():
        data = json.loads(sf.read_text(encoding="utf-8"))
        out["freshness"] = [
            {
                "source": r["unique_id"].split(".", 2)[-1],
                "status": r["status"],
                "max_loaded_at": (r.get("max_loaded_at") or "")[:19].replace("T", " "),
                "age_hours": round(r.get("max_loaded_at_time_ago_in_s", 0) / 3600, 1),
            }
            for r in data["results"]
        ]
        out["freshness_at"] = data["metadata"]["generated_at"][:19].replace("T", " ") + " UTC"
    return out


def _last_run(runs: Path) -> dict | None:
    """Most recent pipeline run summary that ran more than the report step itself. The run that is
    building this site right now (PIPELINE_RUN_ID) counts too: pipeline.run writes it as 'running'."""
    current = os.environ.get("PIPELINE_RUN_ID", "")
    for path in sorted(runs.glob("*.json"), reverse=True) if runs.exists() else []:
        run = json.loads(path.read_text(encoding="utf-8"))
        if run.get("status") in ("ok", "failed") or (run.get("status") == "running" and current
                                                       and run.get("run_id") == current):
            return run
    return None


def load_dealer_watch(con: duckdb.DuckDBPyConnection, last_month: date,
                      dealers: pd.DataFrame) -> tuple[pd.DataFrame | None, dict, pd.DataFrame]:
    """(scorecard rows for last_month, seed rules, dealers with is_watch_loss_ratio / is_watch merged in).
    Without the dealer mart the frame is None (the overview shows n/a) and no dealer is flagged: there is
    never a second, Python-side rule."""
    from reports.dealer import watch  # local import: reports.dealer imports reports.build, which imports this module

    try:
        cur, rule = watch.scorecard_month(con, last_month), watch.rules(con)
    except duckdb.Error:
        return None, {}, dealers.assign(is_watch_loss_ratio=False, is_watch=False)
    flags = cur.groupby("dealer_number", as_index=False)[["is_watch_loss_ratio", "is_watch"]].max()
    out = dealers.merge(flags.rename(columns={"dealer_number": "dealer_code"}), on="dealer_code", how="left")
    out[["is_watch_loss_ratio", "is_watch"]] = out[["is_watch_loss_ratio", "is_watch"]].fillna(False).astype(bool)
    return cur, rule, out


def load(con: duckdb.DuckDBPyConnection, root: Path, publish_dir: Path) -> ReportData:
    as_of = pd.Timestamp(con.execute("select as_of_date from silver.int_as_of_date").fetchone()[0]).date()
    month0 = pd.Timestamp(as_of).to_period("M").to_timestamp()
    last_month = (month0 - pd.offsets.MonthBegin(1)).date()
    t12_start = (pd.Timestamp(last_month) - pd.offsets.MonthBegin(11)).date()

    portfolio = load_portfolio(con, last_month)
    dealer_monthly = load_dealer_monthly(con, last_month)
    dealers = load_dealers(con, dealer_monthly, t12_start, last_month)
    dealer_watch, dealer_watch_rules, dealers = load_dealer_watch(con, last_month, dealers)

    cancel_cohorts = _df(
        con,
        """
        select date_trunc('quarter', sale_month) as quarter, sum(contracts_sold) as sold,
               sum(contracts_cancelled) as cancelled, sum(cancelled_within_90_days) as early
        from gold.mart_cancellations where sale_month <= ? group by 1 order by 1
        """,
        last_month,
    )
    cancel_cohorts["cancel_rate"] = ratio(cancel_cohorts["cancelled"], cancel_cohorts["sold"])

    return ReportData(
        as_of=as_of,
        last_month=last_month,
        portfolio=portfolio,
        dealer_monthly=dealer_monthly,
        dealers=dealers,
        products=_df(con, "select * from gold.mart_product_performance order by tier_rank, term_months, new_used"),
        cancel_cohorts=cancel_cohorts,
        claims_trend=_df(con, "select * from gold.mart_claims_trend where loss_month <= ?", last_month),
        ibnr=_df(con, "select * from gold_actuarial.ibnr_by_cohort order by cohort_start"),
        triangle=_df(con, "select * from gold_actuarial.loss_triangle order by basis, cohort_start, age_quarters"),
        factors=_df(con, "select * from gold_actuarial.development_factors order by age_quarters"),
        curves=_df(con, "select * from gold_actuarial.earned_premium_by_curve"),
        bench_t12=pd.read_csv(publish_dir / "peer_benchmark_t12.csv", keep_default_na=False, na_values=[""]),
        bench_monthly=pd.read_csv(publish_dir / "peer_benchmark_monthly.csv", keep_default_na=False, na_values=[""]),
        manifest=json.loads((publish_dir / "manifest.json").read_text(encoding="utf-8")),
        quality=load_quality(con, root),
        pnl_monthly=load_pnl_monthly(con),
        dealer_watch=dealer_watch,
        dealer_watch_rules=dealer_watch_rules,
    )
