"""V6 finance pages and sections (docs/V3_DESIGN.md section 9): Experience with a "View by" switch,
New Reserves by term, the commission hierarchy, the reserve bridge and rate-card check on Close
controls, run-off and transfer sections on Cession, and the partner settlement / remit register /
rate card Excel tabs. Reads the ``finance`` marts and ``seeds`` only, like the rest of the package.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from reports import charts
from reports.build import Kpi, Page, Section, chart_section, link, money, num, pct

EXPERIENCE_VIEWS = [  # (anchor label, column, header) in the order of the legacy button bar
    ("By Date", "entered_year", "Entered year"),
    ("By Product", "product_code", "Product"),
    ("By Dealer", "dealer_name", "Dealer"),
    ("By State", "reportable_state", "State"),
    ("By Plan", "plan", "Plan"),
    ("By Make", "vehicle_make", "Make"),
    ("By Effective year", "effective_year", "Effective year"),
    ("By Term", "term_months", "Term (months)"),
]
TOP_N = 25  # long views (dealer, plan, make) show the top rows by net written; the Excel tab has all
LEVEL_LABEL = {"NATIONAL": "National", "REGIONAL": "Regional", "DISTRICT": "District"}


def _df(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> pd.DataFrame:
    return con.execute(sql, params or []).df()


def _month_key(col: str) -> str:
    return f"(year({col}) * 100 + month({col}))"


def load(con: duckdb.DuckDBPyConnection, closed: int, t12: list[int]) -> dict[str, pd.DataFrame]:
    """Frames for the V6 pages, cut at the same closed period as every other finance page."""
    d: dict[str, pd.DataFrame] = {}
    d["experience"] = _df(con, """
        select entered_year, effective_year, product_code, category, plan, dealer_number, dealer_name,
            reportable_state, vehicle_make, term_months, sum(contracts_written) as contracts_written,
            sum(contracts_cancelled) as contracts_cancelled, sum(contracts_net) as contracts_net,
            sum(gross_written) as gross_written, sum(net_written) as net_written, sum(earned) as earned,
            sum(unearned) as unearned, sum(claims_paid) as claims_paid, sum(claims_count) as claims_count
        from finance.mart_experience group by all""")
    d["new_reserves"] = _df(con, f"""
        select category_code, any_value(category) as category, term_bucket, any_value(bucket_label) as bucket_label,
            min(bucket_sort) as bucket_sort, sum(contracts) as contracts, sum(new_reserve) as new_reserve,
            sum(term_months_total) as term_months_total
        from finance.mart_new_reserves_by_term
        where {_month_key('entered_month')} between ? and ?
        group by category_code, term_bucket order by category_code, bucket_sort""", t12)
    d["bridge"] = _df(con, f"""
        select {_month_key('month_start')} as month_key, sum(gross_new_reserve) as gross_new_reserve,
            sum(cancelled_contracts_reserve) as cancelled_contracts_reserve,
            sum(partial_refund_remainder) as partial_refund_remainder, sum(other_adjustments) as other_adjustments,
            sum(net_written_entered) as net_written_entered, sum(timing_in) as timing_in,
            sum(timing_out) as timing_out, sum(net_written_effective) as net_written_effective,
            sum(residual) as residual, sum(contracts_gross) as contracts_gross,
            sum(contracts_cancelled) as contracts_cancelled, sum(contracts_net) as contracts_net
        from finance.mart_reserve_bridge
        where {_month_key('month_start')} <= ?
        group by 1 order by 1 desc""", [closed])
    d["hierarchy"] = _df(con, """
        select level, min(level_sort) as level_sort, node, any_value(parent_node) as parent_node, product_type,
            sum(contracts) as contracts, sum(target_contracts) as target_contracts,
            sum(over_under_contracts) as over_under_contracts, sum(commission) as commission
        from finance.mart_commission_hierarchy where posting_period_key between ? and ?
        group by level, node, product_type order by level_sort desc, node, product_type""", t12)
    d["rate_card"] = _df(con, """
        select * exclude (_built_at, term_sort, band_sort) from finance.mart_rate_card
        order by product_code, plan_code, coverage_level, term_sort, band_sort, class_code, deductible_amount""")
    d["rate_exceptions"] = _df(con, """
        select * exclude (_built_at) from finance.mart_rate_card_exceptions order by entered_date, contract_number""")
    d["settlement"] = _df(con, """
        select * exclude (_built_at) from finance.mart_partner_settlement
        where posting_period_key = ? order by tenant_key, dealer_number""", [closed])
    d["remit_register"] = _df(con, """
        select * exclude (_built_at) from finance.mart_remit_register
        where posting_period_key between ? and ? order by posting_period_key, remit_batch_id, contract_number""", t12)
    return d


def _ratio(n: float, den: float) -> float | None:
    return None if not den or pd.isna(den) else n / den


def _label(v) -> str:
    """A View-by key as text: whole numbers without a trailing .0, missing as Unknown."""
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return "Unknown"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# --------------------------------------------------------------------------- Experience (edit 1)
def experience_view(ex: pd.DataFrame, col: str) -> pd.DataFrame:
    """One View by: the same measures grouped by ``col``; every view sums to the same total."""
    cols = ["net_written", "unearned", "earned", "claims_paid", "contracts_written", "contracts_cancelled",
            "contracts_net", "claims_count"]
    g = ex.assign(_k=ex[col].astype("object").where(ex[col].notna(), "Unknown")).groupby("_k", as_index=False)[
        cols].sum().rename(columns={"_k": col})
    g["loss_ratio"] = g["claims_paid"] / g["earned"].where(g["earned"] != 0)
    g["earned_pct"] = g["earned"] / g["net_written"].where(g["net_written"] != 0)
    g["cancel_pct"] = g["contracts_cancelled"] / g["contracts_written"].where(g["contracts_written"] != 0)
    if col in ("entered_year", "effective_year", "term_months"):
        return g.sort_values(col, key=lambda s: pd.to_numeric(s, errors="coerce"))
    return g.sort_values("net_written", ascending=False)


def page_experience(d: dict, label, lr_status) -> Page:
    ex = d["experience"]
    tot = ex[["net_written", "earned", "unearned", "claims_paid", "contracts_written", "contracts_cancelled",
              "contracts_net", "claims_count"]].sum()
    earned_pct = _ratio(tot["earned"], tot["net_written"])
    cancel_pct = _ratio(tot["contracts_cancelled"], tot["contracts_written"])
    lr = _ratio(tot["claims_paid"], tot["earned"])
    kpis = [
        Kpi("Earned %", pct(earned_pct), "earned / net written reserve"),
        Kpi("Cancel %", pct(cancel_pct), "contracts cancelled / written"),
        Kpi("Loss ratio", pct(lr), "claims paid / earned reserve", lr_status(lr)),
        Kpi("Net written", money(tot["net_written"]), "claims reserve, net of cancels"),
        Kpi("Contracts (net)", num(tot["contracts_net"]), f"{num(tot['contracts_written'])} written"),
    ]
    nav = Section("View by", "Pick a View by. Every view shows the same measures and adds up to the same total.",
                  cols=["View", "Rows"],
                  rows=[[link(lbl, f"finance/experience.html#s{i + 2}"), hdr]
                        for i, (lbl, _, hdr) in enumerate(EXPERIENCE_VIEWS)],
                  table_open=True)
    sections = [nav]
    cols = ["Net written", "Unearned", "Earned", "Claims paid", "Loss ratio", "Contracts (net)", "Claims",
            "Earned %"]
    for lbl, col, hdr in EXPERIENCE_VIEWS:
        v = experience_view(ex, col)
        more = ""
        if col in ("dealer_name", "plan", "vehicle_make") and len(v) > TOP_N:
            more = f" Top {TOP_N} of {len(v)} by net written; the Excel tab Experience has every row."
            v = v.head(TOP_N)
        rows = [[_label(getattr(r, col)), money(r.net_written), money(r.unearned), money(r.earned),
                 money(-r.claims_paid), {"text": pct(r.loss_ratio), "status": lr_status(r.loss_ratio)},
                 num(r.contracts_net), num(r.claims_count), pct(r.earned_pct)] for r in v.itertuples()]
        sections.append(Section(f"{lbl}", f"{hdr}. Claims are shown negative.{more}", cols=[hdr, *cols], rows=rows,
                                wide=True, table_open=True))
    return Page("finance/experience.html", "Experience",
                f"Experience: what was written, what is earned, what was paid. Pick a View by. Inception to date "
                f"at {label}, claims-reserve basis.", kpis, sections)


# --------------------------------------------------------------------------- New Reserves (edit 11)
def page_new_reserves(d: dict, span: str) -> Page:
    nr = d["new_reserves"]
    contracts, reserve = nr["contracts"].sum(), nr["new_reserve"].sum()
    kpis = [
        Kpi("New reserves", money(reserve), f"entered {span}"),
        Kpi("Contract count", num(contracts)),
        Kpi("Avg term months", num(_ratio(nr["term_months_total"].sum(), contracts), 1)),
        Kpi("Avg reserves", money(_ratio(reserve, contracts))),
    ]
    by_cat = nr.groupby(["category_code", "category"], as_index=False)[["new_reserve", "contracts"]].sum() \
        .sort_values("new_reserve", ascending=False)
    s1 = chart_section(
        "New reserves by product category",
        "Gross claims reserve booked at sale on contracts entered in the window, before any cancel.",
        charts.bars(by_cat["category"], {"New reserves": by_cat["new_reserve"]}, yfmt="$~s", hover="$,.0f"),
        "fin-newres-cat", ["Category", "New reserves", "Contracts", "Avg reserve"],
        [[r.category, money(r.new_reserve), num(r.contracts), money(_ratio(r.new_reserve, r.contracts))]
         for r in by_cat.itertuples()])
    buckets = nr[["term_bucket", "bucket_label", "bucket_sort"]].drop_duplicates().sort_values("bucket_sort")
    labels = buckets["bucket_label"].tolist()
    avg_rows, pct_rows = [], []
    for cat, g in nr.groupby("category", sort=True):
        cell = g.set_index("bucket_label")
        total_res, total_n = g["new_reserve"].sum(), g["contracts"].sum()
        avg_rows.append([cat, *[money(_ratio(cell.at[b, "new_reserve"], cell.at[b, "contracts"]))
                                if b in cell.index else "" for b in labels], money(_ratio(total_res, total_n))])
        pct_rows.append([cat, *[pct(_ratio(cell.at[b, "new_reserve"], total_res)) if b in cell.index else ""
                                for b in labels], pct(1.0 if total_res else None)])
    s2 = Section("Average reserves by term year", "Average new reserve per contract, product category x term "
                 "length in years (0-1 = up to 12 months).", cols=["Category", *labels, "Total"], rows=avg_rows,
                 wide=True, table_open=True)
    s3 = Section("% reserves by term year", "Each category's new reserve split by term length (row %).",
                 cols=["Category", *labels, "Total"], rows=pct_rows, wide=True, table_open=True)
    return Page("finance/new-reserves.html", "New Reserves",
                f"New reserves by product and term length, contracts entered {span}. The reserve bridge on Close "
                "controls walks the same measure, inception to date, to the Experience page's net written.",
                kpis, [s1, s2, s3])


# --------------------------------------------------------------------------- Commission hierarchy (edit 12)
def page_commission_hierarchy(d: dict, span: str) -> Page:
    h = d["hierarchy"]
    sections = []
    for level in ("NATIONAL", "REGIONAL", "DISTRICT"):
        g = h[h["level"] == level]
        rows = [[r.node, r.parent_node or "", r.product_type, num(r.contracts),
                 num(r.target_contracts) if pd.notna(r.target_contracts) else "no target",
                 {"text": f"{r.over_under_contracts:+,.0f}" if pd.notna(r.over_under_contracts) else "n/a",
                  "status": "" if pd.isna(r.over_under_contracts) else
                  ("good" if r.over_under_contracts >= 0 else "warning")},
                 money(r.commission)] for r in g.itertuples()]
        sections.append(Section(f"{LEVEL_LABEL[level]} commission",
                                "Contracts entered against the volume target (seed commission_volume_targets), "
                                "Over(+) / Under(-), and commission posted.",
                                cols=["Node", "Rolls up to", "Product type", "Contracts", "Target", "Over / under",
                                      "Commission"], rows=rows, wide=True, table_open=True))
    nat = h[h["level"] == "NATIONAL"]
    kpis = [
        Kpi("Commission, T12", money(nat["commission"].sum()), "ties to the commission ledger"),
        Kpi("Contracts, T12", num(nat["contracts"].sum())),
        Kpi("Over / under target", f"{nat['over_under_contracts'].sum():+,.0f}", "contracts, all product types"),
    ]
    return Page("finance/commission-hierarchy.html", "Commission hierarchy",
                f"Commission by district, region and nation, against volume targets, {span}. Field leadership sees "
                "its own level through the app audience on top of row-level security.", kpis, sections)


# --------------------------------------------------------------------------- sections on existing pages
def bridge_section(d: dict, money_cents) -> Section:
    """Close controls (edit 2): New Reserves -> Experience, step by step, ITD at the closed period."""
    b = d["bridge"]
    g = lambda c: float(b[c].sum()) if len(b) else 0.0  # noqa: E731
    status = lambda v: {"text": money_cents(v), "status": "good" if abs(v) < 0.005 else "critical"}  # noqa: E731
    rows = [["1. Gross new reserve (New Reserves page)", money_cents(g("gross_new_reserve")),
             num(g("contracts_gross"))],
            ["2. Less: sale reserve on contracts since cancelled", money_cents(g("cancelled_contracts_reserve")),
             num(-g("contracts_cancelled"))],
            ["3. Plus: what cancelled contracts still hold (partial refunds)",
             money_cents(g("partial_refund_remainder")), ""],
            ["4. Plus: amendments and reversed cancels", money_cents(g("other_adjustments")), ""],
            ["= Net written by entered month (Experience page)", money_cents(g("net_written_entered")),
             num(g("contracts_net"))],
            ["5. Timing: entered vs effective date", money_cents(g("timing_in") + g("timing_out")), ""],
            ["= Net written by effective month", money_cents(g("net_written_effective")), ""],
            ["Residual (must be $0.00)", status(g("residual")), ""]]
    return Section("Reserve bridge: New Reserves to Experience",
                   "Why two reports show different reserve totals, step by step, inception to date. Each line is "
                   "computed on its own from the ledger; the dbt test assert_reserve_bridge_closes fails the build "
                   "if the residual is not zero or either end differs from its page.",
                   cols=["Step", "Claims reserve", "Contracts"], rows=rows, wide=True, table_open=True)


def rate_card_section(d: dict, money_cents) -> Section:
    ex = d["rate_exceptions"]
    rows = [[r.contract_number, r.exception_type, money_cents(r.booked_coverage_cost),
             money_cents(r.surcharge_amount + r.options_amount), money_cents(r.rate_card_cost),
             money_cents(r.difference)] for r in ex.head(TOP_N).itertuples()]
    if not rows:
        rows = [["None", "every Admin Core contract was priced from the rate card", "", "", "", ""]]
    return Section("Booked coverage cost vs the rate card",
                   f"Admin Core contracts whose booked cost, less surcharges and options, is not the card rate of "
                   f"the rate they were booked on: {len(ex)}. The full card ({num(len(d['rate_card']))} rates) is the "
                   "Excel tab Rate_Card, sorted by term first, then odometer band.",
                   cols=["Contract", "Exception", "Booked", "Surcharges + options", "Card rate", "Difference"],
                   rows=rows, wide=True, table_open=True)


def runoff_sections(d: dict, structure_label: dict) -> list[Section]:
    """Cession (edit 13): run-off entities still earn and pay claims; new business goes to the obligor."""
    ce = d["cession"]
    if "entity_status" not in ce:
        return []
    ro = ce[ce["entity_status"] == "RUNOFF"]
    s1 = Section("Run-off details",
                 "Run-off entities write no new business but still earn and pay claims on the contracts they "
                 "took before the run-off date.",
                 cols=["Entity", "Structure", "Cession type", "GL class", "Run-off date", "Contracts", "Earned, ITD",
                       "Incurred, ITD", "Unearned", "Reserve balance"],
                 rows=[[r.entity_name, structure_label.get(r.entity_type, r.entity_type), r.cession_type or "",
                        r.gl_class_code or "", str(pd.Timestamp(r.runoff_date).date()) if pd.notna(r.runoff_date)
                        else "", num(r.itd_contracts_written), money(r.itd_earned_reserve),
                        money(r.itd_incurred_claims), money(r.unearned_reserve), money(r.reserve_balance)]
                       for r in ro.itertuples()] or [["No entity is in run-off", "", "", "", "", "", "", "", "", ""]],
                 wide=True, table_open=True)
    s2 = Section("Transfer summary",
                 "Contracts entered after an entity's run-off date: their claims reserve stays with the obligor, so "
                 "they are not in the entity's experience above.",
                 cols=["Entity", "Run-off date", "Contracts transferred, ITD", "Reserve kept by the obligor, ITD"],
                 rows=[[r.entity_name, str(pd.Timestamp(r.runoff_date).date()) if pd.notna(r.runoff_date) else "",
                        num(r.itd_contracts_transferred), money(r.itd_reserve_transferred)]
                       for r in ro.itertuples()] or [["No transfers", "", "", ""]],
                 wide=True, table_open=True)
    return [s1, s2]


def settlement_section(d: dict, label: str) -> Section:
    st = d["settlement"]
    cols = ["admin_premium_due", "program_fee_adjustment", "cancel_fees", "transient_claims",
            "obligor_claims_deducted", "net_due"]
    t = st[cols].sum() if len(st) else pd.Series(0.0, index=cols)
    return Section(f"Partner settlement statement, {label}",
                   "What the partner pays from: admin / premium, the insurer-program fee on its own line (it funds "
                   "incentives), cancel fees, claims on contracts not yet remitted, less obligor claims deducted. "
                   "Per dealer in the Excel tab Partner_Settlement.",
                   cols=["Line", "Amount"],
                   rows=[["Admin / premium due", money(t["admin_premium_due"])],
                         ["Insurer-program fee adjustment", money(t["program_fee_adjustment"])],
                         ["Cancellation fees", money(t["cancel_fees"])],
                         ["Transient claims", money(t["transient_claims"])],
                         ["Less: obligor claims deducted", money(-t["obligor_claims_deducted"])],
                         ["Net due", money(t["net_due"])]])


def control_sections(d: dict, money_cents) -> list[Section]:
    """Close controls additions; empty when the V6 frames are not loaded (page logic tests)."""
    return [bridge_section(d, money_cents), rate_card_section(d, money_cents)] if "bridge" in d else []


def settlement_sections(d: dict, label: str) -> list[Section]:
    return [settlement_section(d, label)] if "settlement" in d else []


def index_rows() -> list[tuple[str, str, str]]:
    return [
        ("finance/experience.html", "Experience", "Written, earned and paid, with a View by switch"),
        ("finance/new-reserves.html", "New Reserves", "New reserves by product category and term length"),
        ("finance/commission-hierarchy.html", "Commission hierarchy",
         "District, regional and national commission against volume targets"),
    ]


def experience_by_view(ex: pd.DataFrame) -> pd.DataFrame:
    """Every View by stacked in one long tab (view, value, measures): every row of every view, compact."""
    parts = []
    for lbl, col, _ in EXPERIENCE_VIEWS:
        v = experience_view(ex, col)
        parts.append(v.assign(view=lbl, value=v[col].map(_label)).drop(columns=[col]))
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return out[["view", "value", *[c for c in out.columns if c not in ("view", "value")]]] if len(out) else out


def sheets(d: dict) -> dict[str, pd.DataFrame]:
    """Excel tabs, after the existing outputs. Row-level exports keep the same columns every month."""
    return {"Experience": experience_by_view(d["experience"]), "New_Reserves": d["new_reserves"],
            "Reserve_Bridge": d["bridge"],
            "Commission_Hierarchy": d["hierarchy"], "Rate_Card": d["rate_card"],
            "Rate_Card_Exceptions": d["rate_exceptions"], "Partner_Settlement": d["settlement"],
            "Remit_Register": d["remit_register"]}

