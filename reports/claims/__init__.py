"""Claims KPI report package (owner: Senior Data Analyst, dealer & claims).

Plug-in contract (docs/V3_DESIGN.md section 3): ``build(con, out_dir) -> list[Page]``. Reads the
``claims`` marts only (all derived from ``core.fct_claim_payment_line``) and writes
``<out_dir>/downloads/claims/claims_kpi.xlsx``.

KPIs: frequency, severity, denial rate, early claims (30-180 days from effective), open claims aged
30+, top components and turnaround. Formulas: dbt/models/metrics.yml and docs/METRICS.md.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from reports import charts
from reports.build import Kpi, Page, Section, chart_section, link, money, month_label, num, pct
from reports.dealer import as_of, t12_start
from reports.dealer.excel import write_workbook

AREA = "claims"
PAGE = "claims/kpi.html"
NAV_LABEL = "Claims KPIs"
WINDOWS = [30, 60, 90, 120, 150, 180]
# The claims page shows a share of CLAIMS; the Actuarial page shows an incidence per CONTRACT. Two names.
EARLY_SHARE_LABEL = "Early-claim share of claims"
SUMS = ["exposure_years", "claims_reported", "claims_denied", "claim_count", "paid_claim_count", "open_claims",
        "open_claims_aged_30_plus", "paid_amount_on_paid_claims", "paid_amount", "incurred_amount",
        "turnaround_days_total", "turnaround_claims", *[f"early_claims_{w}" for w in WINDOWS]]


def _div(a, b):
    """Elementwise or scalar a / b, NaN where b is 0 (same rule as the safe_divide macro)."""
    if isinstance(b, pd.Series):
        return a / b.where(b != 0)
    return a / b if b else float("nan")


def kpis_from(df: pd.DataFrame) -> pd.DataFrame | pd.Series:
    """Re-derive the ratio KPIs from summed components (never average a ratio)."""
    out = df.copy()
    out["claim_frequency"] = _div(out["claim_count"], out["exposure_years"])
    out["claim_severity"] = _div(out["paid_amount_on_paid_claims"], out["paid_claim_count"])
    out["denial_rate"] = _div(out["claims_denied"], out["claims_reported"])
    out["avg_turnaround_days"] = _div(out["turnaround_days_total"], out["turnaround_claims"])
    return out


def build(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list:
    _, last_month = as_of(con)
    kpi = con.execute("select * from claims.mart_claims_kpi").df()
    kpi["month_start"] = pd.to_datetime(kpi["month_start"])
    comp = con.execute("select * from claims.mart_claim_components").df()
    comp["month_start"] = pd.to_datetime(comp["month_start"])
    open_claims = con.execute(
        "select open_age_band, count(*) as claims, sum(incurred_amount) as incurred, max(open_age_days) as oldest "
        "from claims.mart_claim_summary where status_group = 'open' group by 1 order by min(open_age_days)"
    ).df()

    start = t12_start(last_month)
    t12 = kpi[(kpi["month_start"] >= start) & (kpi["month_start"] <= last_month)]
    tot = kpis_from(t12[SUMS].sum().to_frame().T).iloc[0]
    all_time = kpis_from(kpi[SUMS].sum().to_frame().T).iloc[0]
    head_w = 90
    early_share = _div(tot[f"early_claims_{head_w}"], tot["claim_count"])
    aged = int(all_time["open_claims_aged_30_plus"])
    # Claims with no matched contract are counted here; the V1 Claims & IBNR page leaves them out.
    unmatched = int(t12["claims_unmatched"].sum()) if "claims_unmatched" in t12 else 0
    basis = "by loss month" + (f", incl. {num(unmatched)} with no matched contract" if unmatched else "")

    kpis = [
        Kpi("Claim frequency, T12", num(tot["claim_frequency"], 3), "claims per earned contract-year"),
        Kpi("Claim severity, T12", money(tot["claim_severity"]), "paid per paid claim"),
        Kpi("Denial rate, T12", pct(tot["denial_rate"]), f"{num(tot['claims_denied'])} of "
            f"{num(tot['claims_reported'])} reported ({basis})"),
        Kpi(f"{EARLY_SHARE_LABEL} ({head_w} days), T12", pct(early_share),
            f"{num(tot[f'early_claims_{head_w}'])} non-denied claims with a loss within {head_w} days of "
            "effective, of non-denied claims (not the Actuarial page's per-contract incidence)",
            "warning" if early_share > 0.10 else "good"),
        Kpi("Open claims aged 30+", num(aged), f"of {num(all_time['open_claims'])} open today",
            "warning" if aged else "good"),
        Kpi("Average turnaround, T12", f"{num(tot['avg_turnaround_days'], 1)} days", "reported to closed"),
    ]

    monthly = kpis_from(kpi[(kpi["month_start"] > last_month - pd.DateOffset(months=36))
                            & (kpi["month_start"] <= last_month)].groupby("month_start")[SUMS].sum()).reset_index()
    s1 = chart_section(
        "Claim frequency by loss month",
        "Claims per contract-year of coverage, by the month the breakdown happened. A rising line means each "
        "contract is producing more claims. The latest months dip because some claims are not reported yet.",
        charts.lines(monthly, "month_start", {"claim_frequency": "Frequency"}, yfmt=".2f", hover=".3f"),
        "cl-kpi-freq", ["Month", "Claims", "Exposure years", "Frequency"],
        [[month_label(r.month_start), num(r.claim_count), num(r.exposure_years), num(r.claim_frequency, 3)]
         for r in monthly.itertuples()][::-1],
    )
    s2 = chart_section(
        "Severity and denial rate by loss month",
        "Severity is the average paid on a paid claim. Denial rate is the share of reported claims that were "
        "denied (not covered, waiting period, pre-existing...).",
        charts.lines(monthly, "month_start", {"claim_severity": "Severity"}, yfmt="$,.0f", hover="$,.0f"),
        "cl-kpi-sev", ["Month", "Severity", "Denial rate"],
        [[month_label(r.month_start), money(r.claim_severity), pct(r.denial_rate)] for r in monthly.itertuples()][::-1],
    )
    early = pd.DataFrame({"window": [f"{w} days" for w in WINDOWS],
                          "claims": [tot[f"early_claims_{w}"] for w in WINDOWS]})
    early["share"] = early["claims"] / max(tot["claim_count"], 1)
    s3 = chart_section(
        f"{EARLY_SHARE_LABEL}: loss within N days of the contract start (T12)",
        "A claim soon after the sale can mean a pre-existing problem or a waiting period not enforced. "
        "Each bar includes the shorter windows.",
        charts.bars(early["window"], {"Share of claims": early["share"]}, yfmt=".0%", hover=".1%"),
        "cl-kpi-early", ["Window", "Claims", "Share of claims"],
        [[r.window, num(r.claims), pct(r.share)] for r in early.itertuples()],
    )
    ct = comp[(comp["month_start"] >= start) & (comp["month_start"] <= last_month) & (comp["status_group"] != "denied")]
    top = (ct.groupby("component_code")[["claims", "component_cost"]].sum()
           .sort_values("component_cost", ascending=False).head(10).reset_index())
    top["label"] = top["component_code"].str.replace("_", " ").str.title()
    s4 = chart_section(
        "Top 10 components by cost (T12)",
        "The parts that drive claim cost (parts plus labor), excluding denied claims.",
        charts.bars(top["label"], {"Cost": top["component_cost"]}, horizontal=True, yfmt="$~s", hover="$,.0f",
                    height=380),
        "cl-kpi-comp", ["Component", "Claims", "Cost", "Cost per claim"],
        [[r.label, num(r.claims), money(r.component_cost), money(r.component_cost / max(r.claims, 1))]
         for r in top.itertuples()], wide=True,
    )
    by_sub = kpis_from(t12.groupby("subcategory_code")[SUMS].sum()).reset_index()
    by_sub = by_sub[by_sub["claims_reported"] > 0].sort_values("claims_reported", ascending=False)
    s5 = Section(
        "KPIs by product subcategory (T12)",
        "Same KPIs, split by product. Ratios are recomputed from summed counts, never averaged.",
        cols=["Subcategory", "Reported", "Frequency", "Severity", "Denial rate", f"Early share {head_w}d",
              "Turnaround (days)"],
        rows=[[r.subcategory_code, num(r.claims_reported), num(r.claim_frequency, 3), money(r.claim_severity),
               pct(r.denial_rate), pct(_div(getattr(r, f"early_claims_{head_w}"), r.claim_count)),
               num(r.avg_turnaround_days, 1)] for r in by_sub.itertuples()],
        wide=True, table_open=True,
    )
    s6 = Section(
        "Open claims by age",
        "Claims still open today, by days since they were reported. Anything 30 days or older needs an adjuster "
        "follow-up.",
        cols=["Age", "Open claims", "Incurred", "Oldest (days)"],
        rows=[[r.open_age_band or "n/a", num(r.claims), money(r.incurred), num(r.oldest)]
              for r in open_claims.itertuples()],
        table_open=True,
        note="Sources: claims.mart_claims_kpi, mart_claim_components, mart_claim_summary (all from "
             "core.fct_claim_payment_line). Rules: seed dealer_claim_kpi_rules.",
    )
    xlsx = write_workbook(out_dir / "downloads" / AREA / "claims_kpi.xlsx", {
        "Monthly": monthly, "By subcategory T12": by_sub, "Top components T12": top.drop(columns=["label"]),
        "Open aging": open_claims, "KPI cube": kpi,
    }, notes=[f"Claims KPIs through {month_label(last_month)} (synthetic data).",
              "Frequency = non-denied claims / exposure years; severity = paid / paid claims; "
              "denial rate = denied / reported."])
    s7 = Section("Download", "Every table on this page, plus the full KPI cube, as an Excel workbook.",
                 cols=["Workbook", "Sheets"],
                 rows=[[link(xlsx.name, xlsx.relative_to(out_dir).as_posix()),
                        "Monthly, By subcategory, Top components, Open aging, KPI cube"]], table_open=True)
    page = Page(PAGE, "Claims KPIs",
                "How often contracts claim, what a claim costs, how many are denied or early, and how fast "
                "claims are closed.", kpis, [s1, s2, s3, s4, s5, s6, s7])
    return [page]
