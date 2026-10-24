"""Assemble every page of the static report site from ``ReportData``.

Each page is a list of KPI tiles and sections. A section is a chart card (Plotly div + a
plain-English "what this means" line + a data table for accessibility) or a table card. One
generic template (``templates/page.html.j2``) renders them all.
"""

from __future__ import annotations

import importlib
import importlib.util
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pandas as pd
import plotly.offline
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from reports import charts
from reports.data import ReportData, ratio

HERE = Path(__file__).resolve().parent
TIERS = ["Powertrain", "Gold", "Platinum"]
HIGH_LR = 0.80  # chart reference line / colour band only. Dealer watch flags and counts never use it: they come
# from dealer.mart_dealer_scorecard and seed dealer_claim_kpi_rules (see dealer_lr_watch below).


def dealer_lr_watch(d: ReportData) -> tuple[float, str]:
    """(loss-ratio watch threshold, 'T12, minimum $10K earned claims reserve') from the dealer seed."""
    from reports.dealer.watch import min_volume_text  # local: reports.dealer imports this module

    rules = getattr(d, "dealer_watch_rules", None) or {}
    return rules.get("scorecard_watch_loss_ratio", HIGH_LR), min_volume_text(rules) if rules else "T12"


# --------------------------------------------------------------------------- formatting
def money(v: float | None) -> str:
    if v is None or pd.isna(v):
        return "n/a"
    # Sign goes before the dollar sign: -$5,689, never $-5,689. Rounding to zero drops the sign.
    a = abs(v)
    if a >= 1e6:
        body = f"{a / 1e6:,.1f}M"
    elif a >= 1e4:
        body = f"{a / 1e3:,.0f}K"
    else:
        body = f"{a:,.0f}"
    sign = "-" if v < 0 and body.strip("0.,MK") else ""
    return f"{sign}${body}"


def no_negative_zero(text: str) -> str:
    """'-0.0%' -> '0.0%', '-0' -> '0': a tiny negative that rounds to zero is shown as zero."""
    return text[1:] if text.startswith("-") and not any(ch in "123456789" for ch in text) else text


def pct(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None or pd.isna(v) else no_negative_zero(f"{v:.{digits}%}")


def num(v: float | None, digits: int = 0) -> str:
    return "n/a" if v is None or pd.isna(v) else no_negative_zero(f"{v:,.{digits}f}")


def month_label(ts) -> str:
    return pd.Timestamp(ts).strftime("%b %Y")


# --------------------------------------------------------------------------- page model
@dataclass
class Kpi:
    label: str
    value: str
    sub: str = ""
    status: str = ""  # "", good, warning, critical (rendered with icon + word, never color alone)
    spark: list[float] = field(default_factory=list)  # optional monthly series, oldest first, drawn as a sparkline


@dataclass
class Section:
    title: str
    caption: str
    chart: str = ""
    cols: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    wide: bool = False
    table_open: bool = False  # table shown directly (no chart) instead of behind "Show data"
    note: str = ""
    html: str = ""  # trusted shell-built markup (the Start here page); never data from a mart


@dataclass
class Page:
    path: str  # relative to site root
    title: str
    lede: str
    kpis: list[Kpi] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    nav: str = ""
    crumb: str = ""  # back-link label for pages with a parent (``nav``); default "All dealers"


def link(text: str, href: str) -> dict:
    return {"text": text, "href": href}


def chart_section(title: str, caption: str, fig, div_id: str, cols: list[str], rows: list[list],
                  wide: bool = False, note: str = "") -> Section:
    return Section(title, caption, charts.to_div(fig, div_id, f"Chart: {title}"), cols, rows, wide, note=note)


SPARK_MONTHS = 24


def spark(series: pd.Series, months: int = SPARK_MONTHS) -> list[float]:
    """Last ``months`` points of a monthly series (oldest first) for a KPI sparkline; display only."""
    vals = pd.to_numeric(series, errors="coerce").dropna().tail(months)
    return [float(v) for v in vals] if len(vals) >= 3 else []


def lr_status(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return "critical" if v > HIGH_LR else ("warning" if v > 0.70 else "good")


# --------------------------------------------------------------------------- executive overview
def overview_money(d: ReportData) -> tuple[list[Kpi], list[Section]]:
    """Premium, loss-ratio and combined-ratio tiles and charts for the executive overview.

    Read from finance.mart_pnl_by_product (``d.pnl_monthly``), the same mart, closed period and formulas as
    the Finance pages, so a label shows the same number everywhere. One vocabulary: "Earned premium" is
    earned gross (all cost-split buckets); "Earned claims reserve" is the loss-ratio denominator.
    """
    f = d.pnl_monthly
    if f is None or len(f) < 13:
        # No V3 P&L mart (e.g. a V1-only warehouse): the V1 portfolio figures, labelled for what they are.
        p = d.portfolio
        now, year_ago = p.iloc[-1], p.iloc[-13]
        lr_delta = (now.t12_loss_ratio - year_ago.t12_loss_ratio) * 100
        return [
            Kpi("Earned claims reserve, T12", money(now.t12_earned_premium), "V1 accident-month basis"),
            Kpi("Loss ratio, T12", pct(now.t12_loss_ratio),
                f"{'up' if lr_delta >= 0 else 'down'} {abs(lr_delta):.1f} pts vs a year ago",
                lr_status(now.t12_loss_ratio)),
        ], []
    now, year_ago = f.iloc[-1], f.iloc[-13]
    lr_delta = (now.t12_loss_ratio - year_ago.t12_loss_ratio) * 100
    window = f"12 posting periods to {month_label(now.month)}"
    kpis = [
        Kpi("Written coverage cost, T12", money(now.t12_written_coverage_cost), window,
            spark=spark(f["t12_written_coverage_cost"])),
        Kpi("Earned premium, T12", money(now.t12_earned_premium), "earned gross, all cost-split buckets",
            spark=spark(f["t12_earned_premium"])),
        Kpi("Earned claims reserve, T12", money(now.t12_earned_claims_reserve), "the loss-ratio denominator",
            spark=spark(f["t12_earned_claims_reserve"])),
        Kpi("Loss ratio, T12", pct(now.t12_loss_ratio),
            f"incurred / earned claims reserve; {'up' if lr_delta >= 0 else 'down'} {abs(lr_delta):.1f} pts "
            "vs a year ago", lr_status(now.t12_loss_ratio), spark=spark(f["t12_loss_ratio"])),
        Kpi("Combined ratio, T12", pct(now.t12_combined_ratio),
            "(claims + acquisition + admin fee) / earned premium",
            "critical" if now.t12_combined_ratio > 1 else "good", spark=spark(f["t12_combined_ratio"])),
    ]
    prem = f[["month", "written_coverage_cost", "earned_premium", "earned_claims_reserve"]]
    s1 = chart_section(
        "Written coverage cost vs earned premium by posting period",
        "Coverage cost is written the month a contract is booked; it turns into earned premium slowly over the "
        "contract term. Claims are measured against the earned claims reserve, one slice of earned premium.",
        charts.lines(prem, "month", {"written_coverage_cost": "Written coverage cost",
                                     "earned_premium": "Earned premium",
                                     "earned_claims_reserve": "Earned claims reserve"},
                     yfmt="$~s", hover="$,.0f"),
        "ov-premium", ["Posting period", "Written coverage cost", "Earned premium", "Earned claims reserve"],
        [[month_label(r.month), money(r.written_coverage_cost), money(r.earned_premium),
          money(r.earned_claims_reserve)] for r in prem.itertuples()][::-1],
        wide=True, note="From finance.mart_pnl_by_product, the same figures as Finance > P&L by product.",
    )
    ratios = f.dropna(subset=["t12_combined_ratio", "t12_loss_ratio"])[["month", "t12_loss_ratio",
                                                                         "t12_combined_ratio"]]
    s2 = chart_section(
        "Trailing-12 loss ratio and combined ratio",
        "Loss ratio: claims incurred over the earned claims reserve. Combined ratio: claims plus acquisition "
        "costs and the admin fee, over all earned premium. Above 100% combined means the book lost money.",
        charts.lines(ratios, "month", {"t12_loss_ratio": "Loss ratio", "t12_combined_ratio": "Combined ratio"},
                     yfmt=".0%", hover=".1%", ref=(1.0, "100% = break-even")),
        "ov-ratios", ["Posting period", "Loss ratio", "Combined ratio"],
        [[month_label(r.month), pct(r.t12_loss_ratio), pct(r.t12_combined_ratio)] for r in ratios.itertuples()][::-1],
    )
    return kpis, [s1, s2]


def page_overview(d: ReportData) -> Page:
    p = d.portfolio
    now = p.iloc[-1]
    cc = d.cancel_cohorts
    cancel_rate = ratio(cc["cancelled"].sum(), cc["sold"].sum())
    early_share = ratio(cc["early"].sum(), cc["cancelled"].sum())
    ibnr = d.ibnr["selected_ibnr"].sum()
    from reports.dealer.watch import CANCEL_RATE_ITD  # local: reports.dealer imports this module

    money_kpis, money_sections = overview_money(d)
    kpis = money_kpis + [
        Kpi("Contracts in force", num(now.contracts_in_force), f"at end of {month_label(now.month)}",
            spark=spark(p["contracts_in_force"])),
        Kpi(CANCEL_RATE_ITD, pct(cancel_rate), f"portfolio; {pct(early_share, 0)} of cancels within 90 days"),
        Kpi("IBNR (actuarial)", money(ibnr), "incurred, not yet reported (excludes unexpired coverage)"),
        overview_dealer_watch_kpi(d),
    ]
    cif = p[["month", "contracts_in_force"]]
    s3 = chart_section(
        "Contracts in force",
        "How many contracts are active at each month end. A growing line means new sales outpace expiries "
        "and cancellations.",
        charts.lines(cif, "month", {"contracts_in_force": "Contracts in force"}),
        "ov-cif", ["Month", "Contracts in force"],
        [[month_label(r.month), num(r.contracts_in_force)] for r in cif.itertuples()][::-1],
    )
    cc = cc.assign(label=pd.to_datetime(cc["quarter"]).dt.to_period("Q").astype(str))
    s4 = chart_section(
        "Cancellation rate by sale quarter",
        "Share of contracts sold in each quarter that have been cancelled so far. Recent quarters look low "
        "only because they have had less time to cancel.",
        charts.bars(cc["label"], {CANCEL_RATE_ITD: cc["cancel_rate"]}, yfmt=".0%", hover=".1%"),
        "ov-cancel", ["Sale quarter", "Sold", "Cancelled", CANCEL_RATE_ITD],
        [[r.label, num(r.sold), num(r.cancelled), pct(r.cancel_rate)] for r in cc.itertuples()][::-1],
        wide=True,
    )
    s5 = overview_dealer_watchlist(d)
    return Page("index.html", "Executive overview",
                "Portfolio health of the F&I product book (VSC, GAP, ancillary and maintenance), "
                f"as of {d.as_of:%B %d, %Y}. Figures run "
                f"through the last complete month ({month_label(d.last_month)}).",
                kpis, [*money_sections, s3, s4, s5])


def overview_dealer_watch_kpi(d: ReportData) -> Kpi:
    """Loss-ratio watch count from the dealer scorecard (same helper as site/dealer/scorecard.html)."""
    lr, basis = dealer_lr_watch(d)
    if d.dealer_watch is None:
        return Kpi(f"Dealers above {pct(lr, 0)} LR", "n/a", "dealer scorecard mart not built")
    from reports.dealer.watch import watch_counts

    n = watch_counts(d.dealer_watch)
    return Kpi(f"Dealers above {pct(lr, 0)} LR", num(n["loss_ratio"]),
               f"{basis}; {num(n['any'])} of {num(n['scored'])} on any scorecard watch",
               "warning" if n["loss_ratio"] else "good")


def overview_dealer_watchlist(d: ReportData) -> Section:
    lr, basis = dealer_lr_watch(d)
    note = Markup('Source: dealer.mart_dealer_scorecard, the same rule as the '
                  '<a href="dealer/scorecard.html">dealer scorecard</a> (which also watches late remittance '
                  'and early claims).')
    cols = ["Dealer", "State", "Loss ratio T12", "Earned claims reserve T12", "LR peer pct", "Flag"]
    if d.dealer_watch is None:
        return Section("Dealer watchlist: highest trailing-12 loss ratio", "Dealer scorecard mart not built.",
                       cols=cols, rows=[], note=note)
    from reports.dealer.watch import lr_watchlist, packet_links

    watch = lr_watchlist(d.dealer_watch, d.dealer_watch_rules.get("scorecard_min_t12_earned", 0.0))
    packets = packet_links(d.dealer_watch)  # the one dealer deliverable: the tenant-scoped packet
    rows = []
    for r in watch.itertuples():
        name = f"{r.dealer_number} {r.dealer_name}" if isinstance(r.dealer_name, str) else str(r.dealer_number)
        rows.append([link(name, packets[r.dealer_number]) if r.dealer_number in packets else name,
                     r.dealer_state, pct(r.t12_loss_ratio), money(r.t12_earned_claims_reserve),
                     pct(r.t12_loss_ratio_peer_pct) if pd.notna(r.t12_loss_ratio_peer_pct) else "n/a",
                     {"status": "critical", "text": f"Above {pct(lr, 0)}"} if r.is_watch_loss_ratio else ""])
    return Section(
        "Dealer watchlist: highest trailing-12 loss ratio",
        f"The ten dealers paying out the largest share of their earned claims reserve in claims, among dealers "
        f"with enough volume to judge ({basis}). Claims count by loss month, so a dealer's figure can differ "
        f"slightly from the posted book ratio above. Flagged dealers are above {pct(lr, 0)}.",
        cols=cols, rows=rows, table_open=True, wide=True, note=note,
    )


# --------------------------------------------------------------------------- product performance
def page_products(d: ReportData) -> Page:
    from reports.dealer.watch import CANCEL_RATE_ITD  # local: reports.dealer imports this module

    pp = d.products
    add = ["contracts_sold", "contracts_cancelled", "earned_premium", "incurred_claims", "paid_claims",
           "claim_count", "paid_claim_count", "exposure_years"]
    by_prod = pp.groupby(["coverage_tier", "term_months"], as_index=False)[add].sum()
    by_prod["loss_ratio"] = ratio(by_prod["incurred_claims"], by_prod["earned_premium"])
    by_tier_nu = pp.groupby(["coverage_tier", "new_used"], as_index=False)[add].sum()
    by_tier_nu["loss_ratio"] = ratio(by_tier_nu["incurred_claims"], by_tier_nu["earned_premium"])
    by_tier = pp.groupby("coverage_tier", as_index=False)[add].sum().set_index("coverage_tier").loc[TIERS]
    by_tier["frequency"] = ratio(by_tier["claim_count"], by_tier["exposure_years"])
    by_tier["severity"] = ratio(by_tier["paid_claims"], by_tier["paid_claim_count"])
    terms = sorted(by_prod["term_months"].unique())

    def lr(df: pd.DataFrame, tier: str, key: str, val) -> float:
        row = df[(df["coverage_tier"] == tier) & (df[key] == val)]
        return float(row["loss_ratio"].iloc[0]) if len(row) else float("nan")

    total = pp[add].sum()
    kpis = [
        Kpi("Loss ratio, inception to date", pct(ratio(total.incurred_claims, total.earned_premium)),
            "all products, accident-month incurred / earned claims reserve"),
        Kpi("Claim frequency", num(ratio(total.claim_count, total.exposure_years), 3), "claims per contract-year"),
        Kpi("Claim severity", money(ratio(total.paid_claims, total.paid_claim_count)), "average paid claim"),
        Kpi(CANCEL_RATE_ITD, pct(ratio(total.contracts_cancelled, total.contracts_sold)), "contracts sold to date"),
    ]
    top = by_prod.loc[by_prod["loss_ratio"].idxmax()]
    nu = by_tier_nu.groupby("new_used")[["incurred_claims", "earned_premium"]].sum()
    nu_gap = (ratio(nu.loc["USED", "incurred_claims"], nu.loc["USED", "earned_premium"])
              - ratio(nu.loc["NEW", "incurred_claims"], nu.loc["NEW", "earned_premium"])) * 100
    s1 = chart_section(
        "Loss ratio by coverage tier and term",
        f"Every product group pays out between {pct(by_prod['loss_ratio'].min(), 0)} and "
        f"{pct(by_prod['loss_ratio'].max(), 0)} of its earned claims reserve in claims; the worst is "
        f"{top.coverage_tier} {top.term_months} months. Long terms still have most premium unearned, so their "
        "ratio keeps moving.",
        charts.bars([f"{t} mo" for t in terms], {t: [lr(by_prod, t, "term_months", m) for m in terms] for t in TIERS},
                    yfmt=".0%", hover=".1%", ref=(HIGH_LR, "80% threshold")),
        "pr-tier-term", ["Term", *TIERS],
        [[f"{m} mo", *[pct(lr(by_prod, t, "term_months", m)) for t in TIERS]] for m in terms], wide=True,
    )
    s2 = chart_section(
        "New vs used vehicles",
        f"Used vehicles run {abs(nu_gap):.0f} points {'above' if nu_gap >= 0 else 'below'} new ones: older cars "
        "break down more, so the same contract pays out more on a used car.",
        charts.bars(TIERS, {nu.title(): [lr(by_tier_nu, t, "new_used", nu) for t in TIERS] for nu in ("NEW", "USED")},
                    yfmt=".0%", hover=".1%"),
        "pr-new-used", ["Tier", "New", "Used"],
        [[t, pct(lr(by_tier_nu, t, "new_used", "NEW")), pct(lr(by_tier_nu, t, "new_used", "USED"))] for t in TIERS],
        wide=True,
    )
    s3 = chart_section(
        "Claim frequency by tier",
        f"How many claims a contract files per year in force. {by_tier['frequency'].idxmax()} claims most often "
        f"({num(by_tier['frequency'].max(), 2)} a year): broader coverage gives more parts to claim on.",
        charts.bars(TIERS, {"Claims per contract-year": by_tier["frequency"]}, yfmt=".2f", hover=".3f"),
        "pr-freq", ["Tier", "Claims per contract-year"], [[t, num(by_tier.loc[t, "frequency"], 3)] for t in TIERS],
    )
    s4 = chart_section(
        "Claim severity by tier",
        f"The average paid claim. {by_tier['severity'].idxmax()} claims cost the most "
        f"({money(by_tier['severity'].max())})"
        + (f"; {low} covers many smaller repairs, which pulls its average down."
           if (low := by_tier["severity"].idxmin()) == by_tier["frequency"].idxmax() else "."),
        charts.bars(TIERS, {"Average paid claim": by_tier["severity"]}, yfmt="$,.0f", hover="$,.0f"),
        "pr-sev", ["Tier", "Average paid claim"], [[t, money(by_tier.loc[t, "severity"])] for t in TIERS],
    )
    tier_of = pp.drop_duplicates("product_code").set_index("product_code")["coverage_tier"]
    cv = d.curves.assign(coverage_tier=d.curves["product_code"].map(tier_of))
    cv = cv.groupby(["curve", "coverage_tier"], as_index=False)[["earned_premium", "incurred_losses"]].sum()
    cv["loss_ratio"] = ratio(cv["incurred_losses"], cv["earned_premium"])
    curve_names = {"rule_of_78s": "Rule of 78s", "pro_rata": "Pro-rata", "reverse_rule_of_78s": "Reverse Rule of 78s"}

    def clr(curve: str, tier: str) -> float:
        row = cv[(cv["curve"] == curve) & (cv["coverage_tier"] == tier)]
        return float(row["loss_ratio"].iloc[0]) if len(row) else float("nan")

    s5 = chart_section(
        "Same claims, three earning curves",
        "The loss ratio depends on how fast premium is counted as earned. Reverse Rule of 78s earns slowly, matching "
        "how VSC claims arrive late in the term, so it shows the most cautious picture.",
        charts.bars(TIERS, {curve_names[c]: [clr(c, t) for t in TIERS] for c in curve_names},
                    yfmt=".0%", hover=".1%"),
        "pr-curves", ["Tier", *curve_names.values()],
        [[t, *[pct(clr(c, t)) for c in curve_names]] for t in TIERS], wide=True,
        note="From gold_actuarial.earned_premium_by_curve (the actuarial module). Pro-rata is the reporting default.",
    )
    s6 = Section(
        "All products", "Every product by vehicle condition, straight from gold.mart_product_performance.",
        cols=["Product", "Condition", "Sold", "Earned claims reserve", "Loss ratio", "Frequency", "Severity",
              "Pure premium", CANCEL_RATE_ITD],
        rows=[[r.product_code, r.new_used.title(), num(r.contracts_sold), money(r.earned_premium), pct(r.loss_ratio),
               num(r.claim_frequency, 3), money(r.claim_severity), money(r.pure_premium), pct(r.cancel_rate)]
              for r in pp.itertuples()],
        table_open=True, wide=True,
    )
    return Page("products.html", "Product performance",
                "Which coverage tiers, terms and vehicle types make or lose money, inception to date.",
                kpis, [s1, s2, s3, s4, s5, s6])


# --------------------------------------------------------------------------- claims & IBNR
def page_claims(d: ReportData) -> Page:
    ib = d.ibnr
    reported, ibnr_total, ult = ib["reported_to_date"].sum(), ib["selected_ibnr"].sum(), ib["selected_ultimate"].sum()
    ct = d.claims_trend.copy()
    ct["loss_month"] = pd.to_datetime(ct["loss_month"])
    t12 = ct[ct["loss_month"] > pd.Timestamp(d.last_month) - pd.DateOffset(months=12)]
    kpis = [
        Kpi("Reported losses", money(reported), "paid + case reserves to date"),
        Kpi("IBNR", money(ibnr_total), f"incurred, not yet reported; most claims are reported within 2 weeks, so "
            f"IBNR is {pct(ratio(ibnr_total, reported), 1)} of reported"),
        Kpi("Losses on unexpired coverage (in UPR)", money(ib["unexpired_risk_losses"].sum()),
            "not IBNR: claims on coverage not yet run, funded by unearned premium"),
        Kpi("Ultimate losses", money(ult), "policy-year basis: reported + IBNR + unexpired coverage"),
        Kpi("Ultimate loss ratio", pct(ratio(ult, ib["ultimate_premium"].sum())), "ultimate / ultimate premium",
            lr_status(ratio(ult, ib["ultimate_premium"].sum()))),
        Kpi("Claims reported, T12", num(t12["claims_reported"].sum()),
            f"denial rate {pct(ratio(t12['claims_denied'].sum(), t12['claims_reported'].sum()))}"),
        Kpi("Valuation date", pd.Timestamp(ib["valuation_date"].iloc[0]).strftime("%b %d, %Y"), "last quarter end"),
    ]
    s1 = chart_section(
        "Reported losses and IBNR by sale cohort",
        "Each bar is the contracts sold in one quarter, projected to the end of their coverage (policy-year "
        "basis), so Reported + IBNR + Unexpired coverage = Ultimate. 'IBNR' is claims that already happened but "
        "are not reported yet. 'Unexpired coverage' is claims expected on coverage that has not run yet; it sits "
        "with the unearned premium, not IBNR. The estimate for young cohorts is mainly unexpired coverage.",
        charts.bars(ib["cohort_label"], {"Reported": ib["reported_to_date"], "IBNR": ib["selected_ibnr"],
                                         "Unexpired coverage": ib["unexpired_risk_losses"]},
                    stacked=True, yfmt="$~s", hover="$,.0f"),
        "cl-ibnr", ["Cohort", "Method", "Reported", "IBNR", "Losses on unexpired coverage (in UPR)", "Ultimate",
                    "Ultimate LR"],
        [[r.cohort_label, r.method.replace("_", " "), money(r.reported_to_date), money(r.selected_ibnr),
          money(r.unexpired_risk_losses), money(r.selected_ultimate), pct(r.ultimate_loss_ratio)]
         for r in ib.itertuples()][::-1],
        wide=True,
    )
    s2 = chart_section(
        "Ultimate loss ratio by sale cohort",
        "Where each cohort is expected to land once every claim is in. Cohorts above the line will lose money for "
        "their reinsurance company.",
        charts.bars(ib["cohort_label"], {"Ultimate loss ratio": ib["ultimate_loss_ratio"]}, yfmt=".0%", hover=".1%",
                    ref=(HIGH_LR, "80% threshold")),
        "cl-ult-lr", ["Cohort", "Ultimate loss ratio"],
        [[r.cohort_label, pct(r.ultimate_loss_ratio)] for r in ib.itertuples()][::-1],
    )
    monthly = ct.groupby(["loss_month", "coverage_tier"], as_index=False)["claims_reported"].sum()
    wide_m = monthly.pivot(index="loss_month", columns="coverage_tier", values="claims_reported").reset_index()
    wide_m = wide_m[["loss_month", *[t for t in TIERS if t in wide_m.columns]]]
    s3 = chart_section(
        "Claims reported per month by tier",
        "Claim volume grows as the book grows and ages. The last few months dip because some claims have not been "
        "reported yet (that gap is the IBNR).",
        charts.lines(wide_m, "loss_month", {t: t for t in TIERS if t in wide_m.columns}),
        "cl-monthly", ["Month", *TIERS],
        [[month_label(r[0]), *[num(v) for v in r[1:]]] for r in wide_m.itertuples(index=False)][::-1],
    )
    rep = t12.groupby("repair_category", as_index=False)[["incurred_amount", "claims_paid"]].sum()
    rep = rep.sort_values("incurred_amount", ascending=False)
    rep["label"] = rep["repair_category"].str.replace("_", " ").str.title()
    s4 = chart_section(
        "Where the claim dollars go (T12)",
        "Incurred claim cost by repair type over the last 12 months. The top few categories drive most of the cost.",
        charts.bars(rep["label"], {"Incurred": rep["incurred_amount"]}, horizontal=True, yfmt="$~s", hover="$,.0f",
                    height=380),
        "cl-repair", ["Repair category", "Incurred", "Paid claims"],
        [[r.label, money(r.incurred_amount), num(r.claims_paid)] for r in rep.itertuples()], wide=True,
    )
    tri = d.triangle[d.triangle["basis"] == "reported"]
    grid = tri.pivot(index="cohort_label", columns="age_quarters", values="cumulative").sort_index()
    ages = list(grid.columns)
    rows = [[c, *[("" if pd.isna(v) else f"{v / 1e3:,.0f}") for v in grid.loc[c]]] for c in grid.index]
    f = d.factors.set_index("age_quarters")
    rows.append([{"text": "Age-to-age", "cls": "strong"}, *[num(f["ata"].get(a), 3) for a in ages]])
    rows.append([{"text": "CDF to ultimate", "cls": "strong"}, *[num(f["cdf"].get(a), 3) for a in ages]])
    s5 = Section(
        "Loss development triangle (reported, $K)",
        "Rows are sale quarters, columns are how many quarters old the cohort is. Reading across a row shows "
        "losses piling up as contracts age; the factors at the bottom are how much each step typically grows.",
        cols=["Cohort", *[f"Q{a}" for a in ages]], rows=rows, wide=True, table_open=True,
        note="Volume-weighted chain-ladder factors from gold_actuarial.development_factors.",
    )
    return Page("claims.html", "Claims and IBNR",
                "What claims are costing, what is still to come, and where each sale cohort should finish.",
                kpis, [s1, s2, s3, s4, s5])


# --------------------------------------------------------------------------- dealers
def page_dealers(d: ReportData) -> Page:
    from reports.dealer.watch import CANCEL_RATE_T12, packet_links

    dl = d.dealers.sort_values("t12_loss_ratio", ascending=False)
    card = d.dealer_watch.set_index("dealer_number") if d.dealer_watch is not None else pd.DataFrame()
    canc = card["t12_cancel_rate"] if "t12_cancel_rate" in card else pd.Series(dtype=float)
    packets = packet_links(d.dealer_watch)
    lr, basis = dealer_lr_watch(d)
    s1 = chart_section(
        "Trailing-12 loss ratio by dealer",
        "Each bar is one dealer's claims as a share of the claims reserve it earned over the last 12 months. "
        f"Dealers past the dotted line are on the loss-ratio watch only if they also meet the minimum volume "
        f"({basis}), the dealer scorecard rule.",
        charts.bars(dl["dealer_code"], {"Loss ratio T12": dl["t12_loss_ratio"]}, horizontal=True, yfmt=".0%",
                    hover=".1%", height=760, ref=(lr, pct(lr, 0))),
        "dl-lr", ["Dealer", "Loss ratio T12"], [[r.dealer_code, pct(r.t12_loss_ratio)] for r in dl.itertuples()],
    )
    s2 = Section(
        "All dealers", "Open a dealer to see its packet: the one dealer deliverable, scoped to its program "
        "(tenant), with T12 production, pending contracts, remittance aging and claims KPIs.",
        cols=["Dealer", "City", "State", "Status", "Loss ratio T12", "Loss ratio ITD", "Frequency T12",
              "Contracts in force", CANCEL_RATE_T12, "Flag"],
        rows=[[link(f"{r.dealer_code} {r.dealer_name}", packets[r.dealer_code]) if r.dealer_code in packets
               else f"{r.dealer_code} {r.dealer_name}", r.city, r.state,
               r.dealer_status, pct(r.t12_loss_ratio), pct(r.itd_loss_ratio), num(r.t12_claim_frequency, 3),
               num(r.contracts_in_force), pct(canc.get(r.dealer_code)),
               {"status": "critical", "text": f"Above {pct(lr, 0)}"} if r.is_watch_loss_ratio else ""]
              for r in d.dealers.itertuples()],
        table_open=True, wide=True,
        note=f"{CANCEL_RATE_T12}: cancelled / written for contracts entered in the trailing 12 months, from "
             "dealer.mart_dealer_scorecard (the same number as the dealer scorecard and the dealer packet).",
    )
    return Page("dealers.html", "Dealer scorecards",
                f"Loss ratio, claims and cancellations for each of the {len(d.dealers)} dealers. Unassigned contracts "
                "(no dealer code in the feed) appear in portfolio totals only.",
                [], [s1, s2])


# The V1 flat dealer pages (site/dealers/<dealer>.html) are retired: each dealer has one deliverable, the
# tenant-scoped packet publish writes to site/dealers/<tenant>/<dealer>/ from dealer.mart_dealer_scorecard.


# --------------------------------------------------------------------------- data quality
def page_quality(d: ReportData) -> Page:
    q = d.quality
    dbt = q["dbt"]
    run = q["last_run"]
    tests = dbt.get("counts", {}).get("test", {})
    n_tests, n_pass = sum(tests.values()), tests.get("pass", 0)
    fresh = dbt.get("freshness", [])
    from reports.ops import last_run_kpi  # lazy: ops imports this module
    kpis = [
        last_run_kpi(run, pd.Timestamp.now(tz="UTC").to_pydatetime()),  # same tile as ops.html
        Kpi("dbt tests passing", f"{n_pass} / {n_tests}" if n_tests else "n/a", dbt.get("generated_at", ""),
            "good" if n_tests and n_pass == n_tests else "critical"),
        Kpi("Source freshness", f"{sum(f['status'] == 'pass' for f in fresh)} / {len(fresh)} fresh" if fresh else "n/a",
            dbt.get("freshness_at", "run dbt source freshness"),
            "good" if fresh and all(f["status"] == "pass" for f in fresh) else ("warning" if fresh else "")),
        Kpi("Data as of", f"{d.as_of:%b %d, %Y}", "latest activity in the source feed"),
    ]
    sections = []
    if run:
        sections.append(Section(
            "Last pipeline run", f"Run {run['run_id']}: every step in order, with timing.",
            cols=["Step", "Status", "Seconds", "Note"],
            rows=[[s["name"], {"status": "good" if s["status"] == "ok" else ("critical" if s["status"] == "failed"
                                                                           else ""), "text": s["status"]},
                   num(s["seconds"], 1), s.get("note", "")] for s in run["steps"]],
            table_open=True))
    if dbt.get("available"):
        counts = dbt["counts"]
        sections.append(Section(
            "dbt build results", f"Models, snapshots and tests from the last dbt build ({dbt['elapsed']}s).",
            cols=["Resource", "Status", "Count"],
            rows=[[k, s, num(n)] for k, v in counts.items() for s, n in v.items()]
            + [[p["id"], {"status": "critical", "text": p["status"]}, p["message"][:80]] for p in dbt["problems"]],
            table_open=True))
    if fresh:
        sections.append(Section(
            "Source freshness", "How recently each raw source table received data (dbt source freshness, run after "
            "every dbt build).",
            cols=["Source", "Status", "Last loaded", "Age (hours)"],
            rows=[[f["source"], {"status": "good" if f["status"] == "pass" else "warning", "text": f["status"]},
                   f["max_loaded_at"], num(f["age_hours"], 1)] for f in fresh],
            table_open=True))
    qr = q.get("quarantine")
    if qr is not None and len(qr):
        n_rej, n_open = int(qr["rows"].sum()), int(qr["open"].sum())
        sections.append(Section(
            "Intake quarantine",
            f"{n_rej:,} rows were rejected by the intake data contracts and kept in quarantine with their raw "
            f"record; {n_open:,} are still open. Replayed = the same key was later accepted from a corrected or "
            "re-sent file; duplicate dropped = an accepted copy exists; recovered = replayed by staging.",
            cols=["Source", "Feed", "Reject reason", "Rows", "Replayed", "Duplicate dropped", "Recovered", "Open",
                  "Sample keys"],
            rows=[[x.source_system, x.source_table, x.reject_reason, num(x.rows), num(x.replayed),
                   num(x.duplicate_dropped), num(x.recovered),
                   {"status": "warning" if x.open else "good", "text": num(x.open)}, x.sample_keys]
                  for x in qr.itertuples()],
            table_open=True, wide=True))
    ing = q["ingest"]
    sections.append(Section(
        "Latest ingest batch per table",
        "Change-tracking and file loads from every source into the raw layer, by watermark (audit.ingest_run).",
        cols=["Source", "Table", "Watermark to", "Rows read", "Loaded", "Quarantined", "Status"],
        rows=[[getattr(x, "source_system", ""), x.table_name, str(x.watermark_to)[:19], num(x.rows_read),
               num(x.rows_loaded), num(x.rows_quarantined),
               {"status": "good" if x.status == "ok" else "critical", "text": x.status}]
              for x in ing.itertuples()],
        table_open=True))
    sections.append(Section(
        "Data issues found and handled",
        "The sources are messy on purpose. These are the problems the pipeline caught.",
        cols=["Check", "Rows"], rows=[[label, num(n)] for label, n in q["flags"]], table_open=True))
    if q.get("layers") is not None and len(q["layers"]):
        sections.append(Section(
            "Rows by layer",
            "Raw keeps every source version; staging keeps the latest version per key; intermediate conforms "
            "the two systems; marts are what reports read. Counted from the dbt manifest after each build.",
            cols=["Layer", "Relations", "Rows"],
            rows=[[x.layer, num(x.relations), num(x.rows)] for x in q["layers"].itertuples()], table_open=True))
        sections.append(Section(
            "Rows by relation", "Every raw source, staging, intermediate, snapshot and mart relation.",
            cols=["Layer", "Schema", "Relation", "Rows"],
            rows=[[x.layer, x.schema_name, x.table_name, num(x.rows)] for x in q["tables"].itertuples()],
            wide=True))
    man = d.manifest
    rules = man["rules"]
    sections.append(Section(
        "Publish layer suppression",
        f"Peer benchmarks hide any cell with fewer than {rules['min_contracts']} contracts, fewer than "
        f"{rules['min_dealers']} dealers, or one dealer above {rules['max_dealer_share']:.0%}, plus complementary "
        "cells so hidden values cannot be worked out from totals.",
        cols=["Table", "Rows", "Suppressed", "By reason"],
        rows=[[name, num(t["rows"]), f"{num(t['suppressed'])} ({pct(ratio(t['suppressed'], t['rows']), 0)})",
               ", ".join(f"{k.replace('_', ' ')} {v:,}" for k, v in t["suppressed_by_reason"].items())]
              for name, t in man["tables"].items()],
        table_open=True, wide=True))
    return Page("quality.html", "Data quality and freshness",
                "Can the numbers be trusted today? Pipeline status, tests, freshness and the data issues handled.",
                kpis, sections)


# --------------------------------------------------------------------------- render
START = "start.html"
NAV = [(START, "Start here"), ("index.html", "Overview"), ("products.html", "Products"),
       ("claims.html", "Claims & IBNR"), ("dealers.html", "Dealers"), ("quality.html", "Data quality")]

# Sidebar sections, in display order. Core pages are placed by path; plug-ins by area name, or by an
# optional ``NAV_GROUP`` attribute on the package. Anything unplaced lands in "Platform".
NAV_GROUP_ORDER = ["Executive", "Finance", "Claims & Actuarial", "Regulatory", "Dealers", "Platform"]
CORE_GROUPS = {"index.html": "Executive", "products.html": "Finance", "claims.html": "Claims & Actuarial",
               "dealers.html": "Dealers", "quality.html": "Platform"}
AREA_GROUPS = {"regulatory": "Regulatory", "dealer": "Dealers", "claims": "Claims & Actuarial",
               "finance": "Finance", "ops": "Platform", "actuarial": "Claims & Actuarial", "semantic": "Platform",
               "legacy": "Platform"}
# Order inside a section: lower first; unlisted links keep discovery order after these.
NAV_RANK = {"finance/index.html": 0, "products.html": 1, "claims.html": 0, "claims/index.html": 1,
            "actuarial.html": 2, "dealers.html": 0, "quality.html": 0, "ops.html": 1, "legacy.html": 60}


def group_nav(nav: Sequence[tuple[str, str]], groups: dict[str, str]) -> tuple[list[tuple[str, str]], list[tuple]]:
    """Split nav entries into (ungrouped top links, [(section, [(href, label), ...]), ...]) for the sidebar."""
    top = [(h, lbl) for h, lbl in nav if h == START]
    buckets: dict[str, list[tuple[str, str]]] = {g: [] for g in NAV_GROUP_ORDER}
    for href, label in nav:
        if href == START:
            continue
        g = groups.get(href) or CORE_GROUPS.get(href) or "Platform"
        buckets.setdefault(g, []).append((href, label))
    for items in buckets.values():
        items.sort(key=lambda it: NAV_RANK.get(it[0], 50))
    return top, [(g, items) for g, items in buckets.items() if items]


# One-line "what you will find" for the Start here page; pages not listed fall back to their lede.
NAV_BLURBS = {
    "index.html": "T12 premium, loss and combined ratio, in-force book and the dealer watchlist on one page.",
    "products.html": "Loss ratio, frequency, severity and cancellations by coverage tier.",
    "claims.html": "Reported losses, IBNR and ultimate loss ratio from the actuarial reserve study.",
    "dealers.html": "Every dealer's production and loss ratio, with a link to its packet.",
    "quality.html": "Can the numbers be trusted today? dbt tests, freshness and the data issues handled.",
    "finance/index.html": "Profit-share, commission, P&L by product and cession statements, with Excel downloads.",
    "regulatory/index.html": "State funded-reserve filings and the market survey, with methodology notes.",
    "dealer/index.html": "Dealer production, pending contracts, remittance aging, surcharge audit and scorecard.",
    "claims/index.html": "Claim frequency, severity, denial rate, aging and turnaround.",
    "ops.html": "The 7 AM SLO, the diagnostics gates and the source-to-raw-to-mart reconciliation.",
    "actuarial.html": "Loss development triangles, IBNR methods and the selected reserve.",
    "semantic.html": "The Power BI-ready semantic model: measures, relationships and row-level security roles.",
    "legacy.html": "The old warehouse rebuilt on purpose: ten questions answered both ways, measured, and fixed.",
}
TOUR = [  # (href, what to look at); a step is shown only when its page was built
    ("index.html", "Read the headline tiles. Every figure has an \u201ci\u201d with its definition."),
    ("finance/index.html", "Open a statement and download its Excel copy; totals tie to the overview."),
    ("claims.html", "See how reported losses grow into ultimate losses (IBNR)."),
    ("regulatory/index.html", "Open a state filing: two reserve bases side by side, with notes."),
    ("dealers.html", "Pick a dealer and open its packet: it shows that dealer\u2019s rows only."),
    ("ops.html", "Check the gates: a failed reconciliation stops the site from publishing."),
]
PROOFS = [  # (claim, explanation, page that shows it)
    ("Reliable", "One command rebuilds everything from an empty folder. Each step fails fast and every run is "
     "logged with its timing against a 7 AM target.", "ops.html"),
    ("Reconciled", "Row counts and dollar totals are tied out from source to raw to marts on every run. Eight "
     "diagnostics gates block publishing when one breaks.", "ops.html"),
    ("Secure", "Customer PII is hashed or dropped at staging. A dealer sees only its own rows, in its packet and "
     "in the semantic model\u2019s row-level security.", "semantic.html"),
]


def _first_sentence(text: str) -> str:
    head = text.split(". ")[0].strip()
    return head if head.endswith(".") or not head else head + "."


def page_start(nav: Sequence[tuple[str, str]], groups: dict[str, str], pages: Sequence[Page],
               packets: int) -> Page:
    """The 'Start here' tour: what the site is, what to click, what it proves, and a link to every section."""
    from markupsafe import escape

    built = {p.path for p in pages}
    ledes = {p.path: p.lede for p in pages}
    _, sections = group_nav(nav, groups)
    label = dict(nav)

    def a(href: str, text: str) -> str:
        return f'<a href="{escape(href)}">{escape(text)}</a>'

    steps = "".join(f'<li><p class="step-title">{a(h, label.get(h, h))}</p><p>{escape(t)}</p></li>'
                    for h, t in TOUR if h in built)
    proofs = "".join(
        f'<li class="proof"><p class="proof-title">{escape(c)}</p><p>{escape(t)}</p>'
        + (f'<p class="proof-link">{a(h, "See it: " + label.get(h, h))}</p>' if h in built else "")
        + "</li>" for c, t, h in PROOFS)
    dirs = "".join(
        f'<div class="dir-group"><h3>{escape(g)}</h3><ul>'
        + "".join(f"<li>{a(h, lbl)}<span>{escape(NAV_BLURBS.get(h) or _first_sentence(ledes.get(h, '')))}</span></li>"
                  for h, lbl in items)
        + "</ul></div>" for g, items in sections)
    what = ("<p>This is the reporting site of a <strong>vehicle service contract (VSC) data platform</strong>: "
            "an admin system, a daily dealer feed, a warehouse, accounting-grade facts, regulatory filings and "
            "per-dealer packets, rebuilt every day by one command.</p>"
            "<p>The menu groups the reports by who uses them. Every chart has a <em>Show data</em> table, "
            "every tile has an <em>i</em> button with its definition, and the button at the top right switches "
            "between light and dark mode.</p>"
            '<p class="synthetic"><strong>All data is synthetic.</strong> A seeded generator invents every dealer, '
            "contract, claim and customer. No real company\u2019s data is used.</p>")
    kpis = [Kpi("Report pages", num(len(built) + 1), "every page in the menu tree, including this one"),
            Kpi("Report sections", num(len(sections)), ", ".join(g for g, _ in sections)),
            Kpi("Dealer packets", num(packets), "one filtered packet per reportable dealer"),
            Kpi("Data", "Synthetic", "generated from a fixed seed; no real customers or companies")]
    return Page(START, "Start here",
                "A two-minute guide to this site: what it is, where to click first, and what it proves.", kpis, [
        Section("What this is", "", html=what, wide=True),
        Section("Take the tour", "Six stops, in this order. Use the menu to come back here at any time.",
                html=f'<ol class="tour">{steps}</ol>'),
        Section("What it proves", "Three promises the platform keeps, and where to check each one.",
                html=f'<ul class="proofs">{proofs}</ul>'),
        Section("Every section", "Each report in the menu, in one line.",
                html=f'<div class="directory">{dirs}</div>', wide=True),
    ])


def build_pages(d: ReportData) -> list[Page]:
    return [page_overview(d), page_products(d), page_claims(d), page_dealers(d), page_quality(d)]


# --------------------------------------------------------------------------- report plug-ins
# docs/V3_DESIGN.md section 3: each package exposes build(con, out_dir) -> list[Page]. The shell
# discovers them, so nobody edits this file to add a report. Order = nav order.
PLUGIN_AREAS: tuple[str, ...] = ("regulatory", "dealer", "claims", "finance", "ops", "actuarial", "semantic",
                                 "legacy")


def discover_plugins(areas: Sequence[str] = PLUGIN_AREAS) -> list[tuple[str, ModuleType]]:
    """Import ``reports.<area>`` for every area that exists. A plug-in that fails to import fails the build."""
    found = []
    for area in areas:
        name = f"reports.{area}"
        if importlib.util.find_spec(name) is None:
            continue
        mod = importlib.import_module(name)
        if callable(getattr(mod, "build", None)):
            found.append((area, mod))
    return found


NAV_AREA_OF: dict[str, str] = {}  # plug-in nav href -> sidebar section, filled by plugin_pages


def plugin_pages(con, out: Path, plugins: Sequence[tuple[str, ModuleType]]) -> tuple[list[Page], list[tuple]]:
    """Run each plug-in's build(); return its pages plus one nav entry per area (its first page)."""
    pages: list[Page] = []
    nav: list[tuple[str, str]] = []
    for area, mod in plugins:
        try:
            got = list(mod.build(con, out) or [])
        except Exception as exc:  # name the area, then fail the build: a broken report never ships quietly
            raise RuntimeError(f"report plug-in reports.{area} failed: {exc}") from exc
        bad = [p for p in got if not isinstance(p, Page)]
        if bad:
            raise TypeError(f"reports.{area}.build must return reports.build.Page objects, got {type(bad[0])}")
        if got:
            nav.append((got[0].path, getattr(mod, "NAV_LABEL", area.title())))
            NAV_AREA_OF[got[0].path] = getattr(mod, "NAV_GROUP", AREA_GROUPS.get(area, "Platform"))
            pages += got
    return pages, nav


def copy_dealer_packets(publish_dir: Path | None, out: Path) -> int:
    """Copy publish's per-dealer packets to ``site/dealers/<tenant>/<dealer>/``; returns the packet count."""
    src = publish_dir / "dealers" if publish_dir else None
    if src is None or not src.is_dir():
        return 0
    shutil.copytree(src, out / "dealers", dirs_exist_ok=True)
    return sum(1 for _ in (out / "dealers").glob("*/*/index.html"))


def spark_svg(values: Sequence[float]) -> Markup:
    return Markup(charts.sparkline(values))


def render_site(d: ReportData, out: Path, con=None, publish_dir: Path | None = None,
                plugins: Sequence[tuple[str, ModuleType]] | None = None) -> list[Path]:
    env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    tpl = env.get_template("page.html.j2")
    if out.exists():
        shutil.rmtree(out)
    (out / "assets").mkdir(parents=True)
    (out / "dealers").mkdir()
    shutil.copy(HERE / "static" / "site.css", out / "assets" / "site.css")
    shutil.copy(HERE / "static" / "site.js", out / "assets" / "site.js")
    (out / "assets" / "plotly.min.js").write_text(plotly.offline.get_plotlyjs(), encoding="utf-8")
    packets = copy_dealer_packets(publish_dir, out)
    extra, extra_nav = ([], [])
    if con is not None:
        extra, extra_nav = plugin_pages(con, out, discover_plugins() if plugins is None else plugins)
    nav = NAV + extra_nav
    groups = {h: NAV_AREA_OF[h] for h, _ in extra_nav if h in NAV_AREA_OF}
    nav_top, nav_sections = group_nav(nav, groups)
    pages = build_pages(d) + extra
    if any(h == START for h, _ in nav):
        pages = [page_start(nav, groups, pages, packets), *pages]
    written = []
    for page in pages:
        depth = page.path.count("/")
        (out / page.path).parent.mkdir(parents=True, exist_ok=True)
        html = tpl.render(
            page=page, root="../" * depth, nav_top=nav_top, nav_sections=nav_sections,
            active=page.nav or page.path, as_of=d.as_of, start=START,
            last_month=month_label(d.last_month), chart=lambda s: Markup(s), sparkline=spark_svg,
        )
        path = out / page.path
        path.write_text(html, encoding="utf-8")
        written.append(path)
    return written
