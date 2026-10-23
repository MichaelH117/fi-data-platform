"""Dealer report package (owner: Senior Data Analyst, dealer & claims).

Plug-in contract (docs/V3_DESIGN.md section 3): ``build(con, out_dir) -> list[Page]``. Reads the
``dealer`` marts only and writes Excel workbooks to ``<out_dir>/downloads/dealer/``.

Pages (all under ``dealer/``):
  index.html            hub: what each report answers, plus the Excel downloads
  production.html       dealer x subcategory x month production pivot
  pending.html          pending contracts the dealer still has to finish (fake customer names)
  surcharge-audit.html  contracts that should or should not carry a vehicle surcharge
  specialty-line.html   RV (with chassis VIN) and powersports contracts
  remittance-aging.html remittance age bands and late fees
  scorecard.html        dealer x month scorecard: production, cancel rate, loss ratio, early claims,
                        remittance timeliness, peer percentile and watch flags
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from reports import charts
from reports.build import Kpi, Page, Section, chart_section, link, money, month_label, num, pct
from reports.dealer.excel import write_workbook
from reports.dealer.watch import CANCEL_RATE_T12, LR_LABEL, min_volume_text, rules, watch_counts

AREA = "dealer"
HUB = "dealer/index.html"
NAV_LABEL = "Dealer ops"
CRUMB = "Dealer reports"
DOWNLOADS = f"downloads/{AREA}"


def _df(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


def as_of(con: duckdb.DuckDBPyConnection) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(as-of date, last complete month start) from the core reporting-date dim."""
    r = con.execute("select as_of_date, last_complete_month from core.dim_reporting_date").fetchone()
    return pd.Timestamp(r[0]), pd.Timestamp(r[1])


def t12_start(last_month: pd.Timestamp) -> pd.Timestamp:
    return last_month - pd.DateOffset(months=11)


def _dealer_label(r) -> str:
    return f"{r.dealer_number} {r.dealer_name}" if isinstance(r.dealer_name, str) else str(r.dealer_number)


def _sub(path: str, title: str, lede: str, kpis: list[Kpi], sections: list[Section]) -> Page:
    return Page(path, title, lede, kpis, sections, nav=HUB, crumb=CRUMB)


# --------------------------------------------------------------------------- production
def page_production(con, out: Path, last_month: pd.Timestamp) -> tuple[Page, Path]:
    prod = _df(con, "select * from dealer.mart_dealer_production")
    prod["month_start"] = pd.to_datetime(prod["month_start"])
    start = t12_start(last_month)
    t12 = prod[(prod["month_start"] >= start) & (prod["month_start"] <= last_month)]
    prior = prod[(prod["month_start"] >= start - pd.DateOffset(months=12)) & (prod["month_start"] < start)]
    growth = t12["contracts_written"].sum() / max(prior["contracts_written"].sum(), 1) - 1
    by_sub = t12.groupby("subcategory_code")["contracts_written"].sum().sort_values(ascending=False)
    active = t12.loc[t12["contracts_written"] > 0, "dealer_key"].nunique()

    kpis = [
        Kpi("Contracts written, T12", num(t12["contracts_written"].sum()),
            f"{'up' if growth >= 0 else 'down'} {abs(growth):.1%} vs prior 12 months"),
        Kpi("Coverage cost, T12", money(t12["coverage_cost"].sum()), "dealer cost of contracts written"),
        Kpi("Dealer markup, T12", money(t12["dealer_markup"].sum()), "retail minus dealer cost"),
        Kpi("Producing dealers, T12", num(active), "wrote at least one contract"),
        Kpi("Top subcategory, T12", by_sub.index[0] if len(by_sub) else "n/a",
            f"{pct(by_sub.iloc[0] / max(by_sub.sum(), 1)) if len(by_sub) else 'n/a'} of contracts"),
    ]

    m24 = prod[prod["month_start"] > last_month - pd.DateOffset(months=24)]
    m24 = m24[m24["month_start"] <= last_month]
    cats = m24.groupby(["month_start", "category_code"])["contracts_written"].sum().unstack(fill_value=0)
    cats = cats[cats.sum().sort_values(ascending=False).index].reset_index()
    cat_cols = [c for c in cats.columns if c != "month_start"]
    s1 = chart_section(
        "Contracts written per month by category",
        "How many contracts dealers sold each month, by product family. Counts every contract written "
        "(pending, in force, cancelled later, or expired); voided and denied contracts are never counted.",
        charts.lines(cats, "month_start", {c: str(c).replace("_", " ").title() for c in cat_cols}),
        "dl-prod-monthly", ["Month", *cat_cols],
        [[month_label(r[0]), *[num(v) for v in r[1:]]] for r in cats.itertuples(index=False)][::-1],
        wide=True,
    )

    pivot = t12.pivot_table(index=["dealer_number", "dealer_name"], columns="subcategory_code",
                            values="contracts_written", aggfunc="sum", fill_value=0)
    pivot = pivot[pivot.sum().sort_values(ascending=False).index]
    pivot["Total"] = pivot.sum(axis=1)
    pivot = pivot.sort_values("Total", ascending=False)
    show_cols = list(pivot.columns[:8]) + (["Total"] if "Total" not in pivot.columns[:8] else [])
    top = pivot.head(25)
    s2 = Section(
        "Production pivot: top 25 dealers x subcategory (T12)",
        "Each cell is contracts written in the last 12 complete months. The Excel download has every dealer, "
        "every subcategory and every month.",
        cols=["Dealer", *[str(c) for c in show_cols]],
        rows=[[f"{idx[0]} {idx[1]}", *[num(top.loc[idx, c]) for c in show_cols]] for idx in top.index],
        wide=True, table_open=True,
        note="Grain: dealer x product subcategory x entered month. Source: dealer.mart_dealer_production.",
    )

    month_pivot = prod.pivot_table(index=["dealer_number", "dealer_name", "subcategory_code"],
                                   columns=prod["month_start"].dt.strftime("%Y-%m"),
                                   values="contracts_written", aggfunc="sum", fill_value=0).reset_index()
    xlsx = write_workbook(out / DOWNLOADS / "dealer_production.xlsx", {
        "Summary T12": pivot.reset_index(),
        "Pivot by month": month_pivot,
        "Production": prod.sort_values(["dealer_number", "subcategory_code", "month_start"]),
    }, notes=[f"Dealer production through {month_label(last_month)} (synthetic data).",
              "Counts contracts written by entered month; status set from seed dealer_report_status_sets."])
    page = _sub("dealer/production.html", "Dealer production",
                "Contracts each dealer wrote, by product subcategory and month.", kpis, [s1, s2])
    return page, xlsx


# --------------------------------------------------------------------------- pending contracts
def page_pending(con, out: Path) -> tuple[Page, Path]:
    pend = _df(con, "select * from dealer.mart_dealer_pending_contracts order by days_pending desc")
    over60 = int((pend["days_pending"] > 60).sum())
    kpis = [
        Kpi("Pending contracts", num(len(pend)), "sold, paperwork or remittance not finished"),
        Kpi("Dealers with pending", num(pend["dealer_key"].nunique())),
        Kpi("Median days pending", num(pend["days_pending"].median()), "since the sale date"),
        Kpi("Pending over 60 days", num(over60), "follow up with the dealer",
            "warning" if over60 else "good"),
        Kpi("Retail value pending", money(pend["retail_price"].sum()), "not yet on the books"),
    ]
    bands = (pend.groupby(["pending_age_band_order", "pending_age_band"]).size()
             .reset_index(name="contracts").sort_values("pending_age_band_order"))
    s1 = chart_section(
        "Pending contracts by age",
        "How long sold contracts have been waiting for the dealer. Old pending contracts are customers who may "
        "think they are covered when the contract is not active yet.",
        charts.bars(bands["pending_age_band"], {"Contracts": bands["contracts"]}),
        "dl-pend-age", ["Age band", "Contracts"],
        [[r.pending_age_band, num(r.contracts)] for r in bands.itertuples()],
    )
    by_dealer = (pend.groupby(["dealer_number", "dealer_name"], dropna=False)
                 .agg(pending=("contract_key", "count"), oldest=("days_pending", "max"),
                      over_60=("days_pending", lambda s: int((s > 60).sum())), retail=("retail_price", "sum"))
                 .reset_index().sort_values(["pending", "oldest"], ascending=False))
    s2 = Section(
        "Dealers with the most pending contracts",
        "Where to call first: dealers with many or very old pending contracts.",
        cols=["Dealer", "Pending", "Oldest (days)", "Over 60 days", "Retail value"],
        rows=[[_dealer_label(r), num(r.pending), num(r.oldest), num(r.over_60), money(r.retail)]
              for r in by_dealer.head(20).itertuples()],
        table_open=True,
    )
    s3 = Section(
        "Oldest 40 pending contracts",
        "Contract-level follow-up list. Customer names are fake (synthetic data); VINs are shown as hashed tokens.",
        cols=["Contract", "Dealer", "Customer", "Product", "Sold", "Lienholder", "Provider", "Days"],
        rows=[[r.contract_number, str(r.dealer_number), f"{r.customer_first_name} {r.customer_last_name}",
               r.product_code, pd.Timestamp(r.date_sold).strftime("%Y-%m-%d"), r.lienholder_name or "Cash",
               r.provider_name or "n/a", num(r.days_pending)] for r in pend.head(40).itertuples()],
        wide=True, table_open=True,
        note="Source: dealer.mart_dealer_pending_contracts (contains_pii: fake names only).",
    )
    xlsx = write_workbook(out / DOWNLOADS / "dealer_pending_contracts.xlsx", {
        "Pending contracts": pend[["administrator_name", "dealer_number", "dealer_name", "contract_number",
                                   "customer_first_name", "customer_last_name", "vin_token", "product_code",
                                   "product", "plan", "date_sold", "effective_date", "lienholder_name",
                                   "provider_name", "retail_price", "days_pending", "pending_age_band"]],
        "By dealer": by_dealer,
    }, notes=["Pending contracts for dealer follow-up. Synthetic data: customer names are invented."])
    page = _sub("dealer/pending.html", "Pending contracts",
                "Contracts sold but not yet active, so each dealer can finish the paperwork.", kpis, [s1, s2, s3])
    return page, xlsx


# --------------------------------------------------------------------------- surcharge audit
def page_surcharge(con, out: Path) -> tuple[Page, Path]:
    aud = _df(con, "select * from dealer.mart_surcharge_audit")
    exc = aud[aud["finding"] != "OK"]
    missing, extra = aud[aud["finding"] == "MISSING"], aud[aud["finding"] == "NOT_EXPECTED"]
    mism = aud[aud["finding"] == "AMOUNT_MISMATCH"]
    kpis = [
        Kpi("Surcharge checks", num(len(aud)), f"{num(aud['contract_key'].nunique())} contracts"),
        Kpi("Exception rate", pct(len(exc) / max(len(aud), 1)), "checks that are not OK",
            "warning" if len(exc) else "good"),
        Kpi("Missing surcharges", num(len(missing)), f"{money(missing['expected_amount'].sum())} under-charged",
            "critical" if len(missing) else "good"),
        Kpi("Charged but not expected", num(len(extra)), f"{money(extra['charged_amount'].sum())} over-charged",
            "warning" if len(extra) else "good"),
        Kpi("Wrong amount", num(len(mism)), f"{money(mism['amount_variance'].abs().sum())} variance"),
    ]
    codes = aud.pivot_table(index="surcharge_code", columns="finding", values="contract_key",
                            aggfunc="count", fill_value=0)
    for f in ["OK", "MISSING", "NOT_EXPECTED", "AMOUNT_MISMATCH"]:
        if f not in codes.columns:
            codes[f] = 0
    codes = codes.reset_index().sort_values("MISSING", ascending=False)
    s1 = chart_section(
        "Exceptions by surcharge type",
        "Each vehicle trait (diesel, 4x4, commercial use, branded title...) must carry a surcharge. 'Missing' "
        "means the dealer did not charge it; 'not expected' means a customer was charged for a trait the "
        "vehicle does not have.",
        charts.bars(codes["surcharge_code"], {"Missing": codes["MISSING"], "Not expected": codes["NOT_EXPECTED"],
                                              "Wrong amount": codes["AMOUNT_MISMATCH"]}, stacked=True),
        "dl-sur-codes", ["Surcharge", "OK", "Missing", "Not expected", "Wrong amount"],
        [[r.surcharge_code, num(r.OK), num(r.MISSING), num(r.NOT_EXPECTED), num(r.AMOUNT_MISMATCH)]
         for r in codes.itertuples()],
    )
    by_dealer = (aud.assign(exception=aud["finding"] != "OK",
                            leak=aud["expected_amount"].where(aud["finding"] == "MISSING", 0.0))
                 .groupby(["dealer_number", "dealer_name"], dropna=False)
                 .agg(checks=("contract_key", "count"), exceptions=("exception", "sum"), leak=("leak", "sum"))
                 .reset_index())
    by_dealer["rate"] = by_dealer["exceptions"] / by_dealer["checks"]
    by_dealer = by_dealer.sort_values(["exceptions", "leak"], ascending=False)
    s2 = Section(
        "Dealers with the most surcharge exceptions",
        "Dealers to retrain on rating. 'Under-charged' is the surcharge revenue that was never collected.",
        cols=["Dealer", "Checks", "Exceptions", "Exception rate", "Under-charged"],
        rows=[[_dealer_label(r), num(r.checks), num(r.exceptions), pct(r.rate), money(r.leak)]
              for r in by_dealer.head(20).itertuples()],
        table_open=True,
    )
    recent = exc.sort_values("contract_date", ascending=False).head(40)
    s3 = Section(
        "Latest 40 exceptions",
        "Contract-level list for correction. VINs are hashed tokens.",
        cols=["Contract", "Dealer", "Date", "VIN token", "Surcharge", "Finding", "Expected", "Charged", "Status"],
        rows=[[r.contract_number, str(r.dealer_number), pd.Timestamp(r.contract_date).strftime("%Y-%m-%d"),
               r.vin_token or "n/a", r.surcharge_code,
               {"status": "critical" if r.finding == "MISSING" else "warning", "text": r.finding.replace("_", " ")},
               money(r.expected_amount), money(r.charged_amount), r.contract_status]
              for r in recent.itertuples()],
        wide=True, table_open=True,
        note="Rules: seed dealer_surcharge_rules. Scope: Admin Core contracts in surcharge-rated subcategories.",
    )
    xlsx = write_workbook(out / DOWNLOADS / "surcharge_audit.xlsx", {
        "Summary": codes, "By dealer": by_dealer,
        "Exceptions": exc.sort_values(["dealer_number", "contract_date"]),
    }, notes=["Surcharge audit: Y/N surcharge found vs expected by vehicle traits (synthetic data)."])
    page = _sub("dealer/surcharge-audit.html", "Surcharge audit",
                "Contracts that should, or should not, carry a vehicle surcharge.", kpis, [s1, s2, s3])
    return page, xlsx


# --------------------------------------------------------------------------- specialty line
def page_specialty(con, out: Path, last_month: pd.Timestamp) -> tuple[Page, Path]:
    sp = _df(con, "select * from dealer.mart_specialty_line order by entered_date desc")
    sp["entered_month"] = pd.to_datetime(sp["entered_month"])
    rv = sp[sp["requires_chassis_vin"]]
    missing = int(sp["is_chassis_vin_missing"].sum())
    kpis = [
        Kpi("Specialty contracts", num(len(sp)), "all time"),
        Kpi("RV contracts", num(len(rv)), f"{num(rv['has_chassis_vin'].sum())} with a chassis VIN"),
        Kpi("RV missing chassis VIN", num(missing), "drivetrain claims cannot be verified",
            "critical" if missing else "good"),
        Kpi("Written last month", num((sp["entered_month"] == last_month).sum()), month_label(last_month)),
        Kpi("Claims paid", money(sp["claims_paid"].sum()), f"{num(sp['claim_count'].sum())} claims"),
    ]
    m = sp[sp["entered_month"] > last_month - pd.DateOffset(months=24)]
    m = m[m["entered_month"] <= last_month]
    monthly = m.groupby(["entered_month", "line_name"]).size().unstack(fill_value=0).reset_index()
    lines_ = [c for c in monthly.columns if c != "entered_month"]
    s1 = chart_section(
        "Specialty contracts written per month",
        "Recreational vehicles and powersports are low volume but high value. An RV has two VINs: the coach and "
        "the chassis it is built on.",
        charts.bars(monthly["entered_month"].dt.strftime("%b %Y"), {c: monthly[c] for c in lines_}, stacked=True),
        "dl-spec-monthly", ["Month", *lines_],
        [[month_label(r[0]), *[num(v) for v in r[1:]]] for r in monthly.itertuples(index=False)][::-1],
        wide=True,
    )
    by_dealer = (sp.groupby(["dealer_number", "dealer_name", "line_name"], dropna=False)
                 .agg(contracts=("contract_key", "count"), purchase=("purchase_price", "sum"),
                      missing=("is_chassis_vin_missing", "sum"), paid=("claims_paid", "sum"))
                 .reset_index().sort_values("contracts", ascending=False))
    s2 = Section(
        "Specialty dealers (all time)",
        "Dealers selling specialty lines, with any RV contracts missing a chassis VIN.",
        cols=["Dealer", "Line", "Contracts", "Purchase price", "Missing chassis VIN", "Claims paid"],
        rows=[[_dealer_label(r), r.line_name, num(r.contracts), money(r.purchase), num(r.missing), money(r.paid)]
              for r in by_dealer.head(20).itertuples()],
        table_open=True,
    )
    s3 = Section(
        "Latest 30 specialty contracts",
        "VIN and chassis VIN are shown as hashed tokens.",
        cols=["Contract", "Status", "Dealer", "Entered", "VIN token", "Chassis VIN token", "Vehicle", "Purchase price"],
        rows=[[r.contract_number, r.status, str(r.dealer_number), pd.Timestamp(r.entered_date).strftime("%Y-%m-%d"),
               r.vin_token or "n/a", r.chassis_vin_token or ("MISSING" if r.requires_chassis_vin else "n/a"),
               f"{r.vehicle_year or ''} {r.vehicle_make or ''} {r.vehicle_model or ''}".strip(),
               money(r.purchase_price)] for r in sp.head(30).itertuples()],
        wide=True, table_open=True, note="Source: dealer.mart_specialty_line. Lines: seed dealer_specialty_lines.",
    )
    cols = ["contract_number", "status", "dealer_number", "dealer_name", "line_name", "product", "entered_date",
            "effective_date", "vin_token", "chassis_vin_token", "has_chassis_vin", "vehicle_make", "vehicle_model",
            "vehicle_year", "purchase_price", "claim_count", "claims_paid"]
    xlsx = write_workbook(out / DOWNLOADS / "specialty_line.xlsx", {
        "All time": sp[cols],
        "Monthly": sp.loc[sp["entered_month"] == last_month, cols],
        "Monthly summary": sp.groupby(["entered_month", "line_name"]).size().reset_index(name="contracts"),
    }, notes=[f"Specialty-line contracts; the Monthly tab is {month_label(last_month)} (synthetic data)."])
    page = _sub("dealer/specialty-line.html", "Specialty line (RV and powersports)",
                "Recreational-vehicle contracts with their chassis VIN, and powersports contracts.", kpis, [s1, s2, s3])
    return page, xlsx


# --------------------------------------------------------------------------- remittance aging
def page_remittance(con, out: Path, last_month: pd.Timestamp) -> tuple[Page, Path]:
    ra = _df(con, "select * from dealer.mart_remittance_aging")
    ra["period"] = pd.to_datetime(ra["posting_period_key"].astype(str) + "01", format="%Y%m%d")
    t12 = ra[(ra["period"] >= t12_start(last_month)) & (ra["period"] <= last_month)]
    n, late = t12["cash_transactions"].sum(), t12["late_transactions"].sum()
    kpis = [
        Kpi("Remitted, T12", money(t12["remitted_amount"].sum()), f"{num(n)} contract remittances"),
        Kpi("Late remittances, T12", pct(late / max(n, 1)), f"{num(late)} late", "warning" if late else "good"),
        Kpi("Late fees, T12", money(t12["late_fees"].sum()), "billed to dealers"),
        Kpi("Average days to remit", num((t12["avg_remit_age_days"] * t12["cash_transactions"]).sum() / max(n, 1), 1),
            "sale entered to cash received"),
    ]
    bands = (t12.groupby(["age_band_order", "age_band"])[["cash_transactions", "remitted_amount"]].sum()
             .reset_index().sort_values("age_band_order"))
    s1 = chart_section(
        "Remittances by age band (T12)",
        "How long dealers took to send the money for contracts they sold. Remittances past the late threshold "
        "carry a late fee.",
        charts.bars(bands["age_band"], {"Remittances": bands["cash_transactions"]}),
        "dl-remit-bands", ["Age band", "Remittances", "Amount"],
        [[r.age_band, num(r.cash_transactions), money(r.remitted_amount)] for r in bands.itertuples()],
    )
    by_dealer = (t12.groupby(["dealer_number", "dealer_name"], dropna=False)
                 [["cash_transactions", "late_transactions", "late_fees", "remitted_amount"]].sum().reset_index())
    by_dealer["late_rate"] = by_dealer["late_transactions"] / by_dealer["cash_transactions"].clip(lower=1)
    by_dealer = by_dealer.sort_values("late_fees", ascending=False)
    s2 = Section(
        "Dealers with the highest late fees (T12)",
        "Slow remitters. Late money means the reserve is funded late.",
        cols=["Dealer", "Remittances", "Late", "Late rate", "Late fees"],
        rows=[[_dealer_label(r), num(r.cash_transactions), num(r.late_transactions), pct(r.late_rate),
               money(r.late_fees)] for r in by_dealer.head(20).itertuples()],
        table_open=True, note="Source: dealer.mart_remittance_aging (core.fct_cash_transaction).",
    )
    xlsx = write_workbook(out / DOWNLOADS / "remittance_aging.xlsx", {
        "By dealer T12": by_dealer, "Detail": ra.drop(columns=["period"]),
    }, notes=["Remittance aging and late fees (synthetic data)."])
    page = _sub("dealer/remittance-aging.html", "Remittance aging",
                "How quickly dealers send in the money for contracts they sell, and the late fees.", kpis, [s1, s2])
    return page, xlsx


# --------------------------------------------------------------------------- scorecard
WATCH_REASONS = [("is_watch_loss_ratio", "Loss ratio"), ("is_watch_late_remit", "Late remittance"),
                 ("is_watch_early_claims", "Early claims (A/E)")]
# Two early-claim numbers, two names (never one label for both). The share is descriptive; the watch of
# record is the A/E test of dealer.mart_dealer_early_claim_ae, the same test as the Actuarial page list.
EARLY_SHARE_LABEL = "Early-claim share of claims, 90d"
EARLY_AE_LABEL = "Early-claim A/E per contract"


def page_scorecard(con, out: Path, last_month: pd.Timestamp) -> tuple[Page, Path]:
    sc = _df(con, "select * from dealer.mart_dealer_scorecard")
    sc["month_start"] = pd.to_datetime(sc["month_start"])
    cur = sc[sc["month_start"] == last_month].copy()
    n = watch_counts(cur)  # the executive overview counts with the same helper
    rule = rules(con)

    def reasons(r) -> str:
        return ", ".join(lbl for col, lbl in WATCH_REASONS if getattr(r, col)) or "-"

    kpis = [
        Kpi("Dealers on watch", num(n["any"]), f"of {num(n['scored'])} scored, T12 to {month_label(last_month)}",
            "warning" if n["any"] else "good"),
        *[Kpi(f"Watch: {lbl.lower()}", num(int(cur[col].sum())),
              min_volume_text(rule) if col == "is_watch_loss_ratio" else "") for col, lbl in WATCH_REASONS],
        Kpi(f"Portfolio {LR_LABEL.lower()}, T12",
            pct(cur["t12_incurred_claims"].sum() / max(cur["t12_earned_claims_reserve"].sum(), 1e-9)),
            "incurred by loss month / earned claims reserve. The overview's posted loss ratio counts claims by "
            "posting period, so the two differ slightly"),
    ]
    ae_days = int(rule.get("scorecard_early_claim_ae_window_days", 180))
    cols = ["Dealer", "Written", CANCEL_RATE_T12, "Loss ratio", "LR peer pct", EARLY_SHARE_LABEL,
            f"{EARLY_AE_LABEL}, {ae_days}d", "Late remits",
            "Avg days late", "Late fees", "Watch"]

    def row(r) -> list:
        return [_dealer_label(r), num(r.t12_contracts_written), pct(r.t12_cancel_rate), pct(r.t12_loss_ratio),
                pct(r.t12_loss_ratio_peer_pct) if pd.notna(r.t12_loss_ratio_peer_pct) else "n/a",
                pct(r.t12_early_claim_rate),
                num(ae, 2) if pd.notna(ae := getattr(r, "early_claim_actual_to_expected", None)) else "n/a",
                pct(r.t12_late_remit_rate),
                num(r.t12_avg_days_late, 1) if pd.notna(r.t12_avg_days_late) else "-", money(r.t12_late_fees),
                {"status": "warning", "text": reasons(r)} if r.is_watch else "-"]

    order = cur.sort_values(["is_watch", "t12_loss_ratio"], ascending=False)
    s1 = Section(
        "Dealer scorecard, trailing 12 months",
        f"One line per dealer. Loss ratio is the {LR_LABEL.lower()}: incurred claims (loss month) over the "
        "earned claims reserve; the peer percentile ranks it among dealers with enough earned claims reserve "
        "(0% best, 100% worst). The early-claim share is the dealer's non-denied claims with a loss within 90 "
        "days of the contract start, as a share of all its claims reported (T12). The early-claim watch uses "
        f"a different, mix-adjusted test: contracts with a first claim (denied included) within {ae_days} days "
        "of the start, actual vs expected for the dealer's own products and new/used mix (A/E). It is the same "
        "test and the same dealer list as the Actuarial page, so a dealer can have a low share and still be on "
        "watch. Late remits are the share of "
        "remittances past the late threshold; days late is the average days past it on those.",
        cols=cols, rows=[row(r) for r in order.head(40).itertuples()], wide=True, table_open=True,
        note="Grain: dealer x month (each block keeps its source basis: entered, earned, loss or posting month). "
             "Watch thresholds: seed dealer_claim_kpi_rules. Source: dealer.mart_dealer_scorecard.",
    )
    trend = (sc[(sc["month_start"] > last_month - pd.DateOffset(months=24)) & (sc["month_start"] <= last_month)]
             .groupby("month_start")["is_watch"].sum().reset_index())
    s2 = chart_section(
        "Dealers on watch per month",
        "How many dealers trip at least one watch rule, month by month (each month looks back 12 months).",
        charts.bars(trend["month_start"].dt.strftime("%b %Y"), {"Dealers on watch": trend["is_watch"]}),
        "dl-score-watch", ["Month", "Dealers on watch"],
        [[month_label(r.month_start), num(r.is_watch)] for r in trend.itertuples()][::-1],
    )
    xlsx = write_workbook(out / DOWNLOADS / "dealer_scorecard.xlsx", {
        "Scorecard T12": order, "Monthly": sc.sort_values(["dealer_number", "month_start"]),
    }, notes=[f"Dealer scorecard through {month_label(last_month)} (synthetic data).",
              "Loss ratio on claims reserve = incurred / earned claims reserve; "
              "cancel rate = cancelled / written (entered-month cohort)."])
    page = _sub("dealer/scorecard.html", "Dealer scorecard",
                "Production, cancellations, loss ratio, early claims and remittance timeliness per dealer.",
                kpis, [s1, s2])
    return page, xlsx


# --------------------------------------------------------------------------- hub + entry point
REPORTS = [
    ("dealer/production.html", "Dealer production", "Contracts written by dealer, subcategory and month."),
    ("dealer/pending.html", "Pending contracts", "Sold contracts still waiting on the dealer."),
    ("dealer/surcharge-audit.html", "Surcharge audit", "Missing or wrongly charged vehicle surcharges."),
    ("dealer/specialty-line.html", "Specialty line", "RV (with chassis VIN) and powersports contracts."),
    ("dealer/remittance-aging.html", "Remittance aging", "Days to remit and late fees by dealer."),
    ("dealer/scorecard.html", "Dealer scorecard", "Loss ratio, cancels, early claims and remittance by dealer."),
]


def build(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list:
    """Build the dealer pages and their Excel workbooks under ``out_dir`` (the site root)."""
    _, last_month = as_of(con)
    built = [page_production(con, out_dir, last_month), page_pending(con, out_dir),
             page_surcharge(con, out_dir), page_specialty(con, out_dir, last_month),
             page_remittance(con, out_dir, last_month), page_scorecard(con, out_dir, last_month)]
    pages = [p for p, _ in built]
    rows = []
    for (path, title, what), (_, xlsx) in zip(REPORTS, built, strict=True):
        rel = xlsx.relative_to(out_dir).as_posix()
        rows.append([link(title, path), what, link(xlsx.name, rel)])
    hub = Page(HUB, "Dealer operations",
               f"Reports the dealer team runs every morning, through {month_label(last_month)}. Each one has an "
               "Excel download for the dealer.",
               [Kpi(p.title, p.kpis[0].value, p.kpis[0].label) for p in pages],
               [Section("Dealer reports", "Open a report, or download its Excel workbook.",
                        cols=["Report", "What it answers", "Excel"], rows=rows, wide=True, table_open=True,
                        note="All dealer reports read the dealer marts only (dbt/models/marts/dealer).")])
    return [hub, *pages]
