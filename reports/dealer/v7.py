"""V7 dealer pages and sections (docs/V3_DESIGN.md 10.4, Financial Analysts):

  agents.html               every store an agent brought in, since the rep's start date (edit 24)
  incentive-statement.html  what we owe each store for each remittance, line by line (edit 46)
  ancillary-pricing.html    what each store charges for add-ons, and its markup (edit 48)
  production.html section   this month: what is still pending, what is active (edit 25)
  specialty-line section    how each RV chassis VIN was typed (edit 27)

Reads the ``dealer`` marts only. A warehouse built before a V7 mart skips that page or section.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from reports.build import Kpi, Page, Section, money, month_label, num, pct
from reports.dealer.excel import write_workbook

DOWNLOADS = "downloads/dealer"
REPORTS = [
    ("dealer/agents.html", "Agents and their stores", "Every store an agent brought in, since a date you choose."),
    ("dealer/incentive-statement.html", "Dealer incentive statement",
     "What we owe each store for each remittance, line by line."),
    ("dealer/ancillary-pricing.html", "Ancillary pricing and markup",
     "What each store charges for add-ons, and its markup."),
]
VIN_STATUS = {  # chassis_vin_status -> (plain label, what to do)
    "valid": ("Typed correctly", "Nothing to do."),
    "lowercase_fixed": ("Typed in lower case (fixed automatically)", "Nothing to do; the check upper-cases it."),
    "missing": ("Missing", "Ask the store for the chassis number."),
    "placeholder": ("A stand-in such as TBD or all zeros", "Ask the store for the real chassis number."),
    "bad_length": ("Wrong length (a VIN has 17 characters)", "Check the number with the store."),
    "duplicate_across_contracts": ("Same number on more than one contract", "Check which contract it belongs to."),
}


def _try(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame | None:
    try:
        return con.execute(sql).df()
    except duckdb.CatalogException:
        return None


def _page(path: str, title: str, lede: str, kpis: list[Kpi], sections: list[Section], hub: str, crumb: str) -> Page:
    return Page(path, title, lede, kpis, sections, nav=hub, crumb=crumb)


# --------------------------------------------------------------------------- agents (edit 24)
def page_agents(con, out: Path, hub: str, crumb: str) -> tuple[Page, Path] | None:
    a = _try(con, "select * exclude (_built_at) from dealer.mart_agent_dealer_production")
    if a is None:
        return None
    a["month_start"] = pd.to_datetime(a["month_start"])
    # the start date is each rep's own first sale: "every store since they started" (no ITD / T12 default)
    start = a.groupby("agent_key")["month_start"].transform("min")
    a = a.assign(rep_start=start)
    pairs = (a.groupby(["agent_key", "agent_code", "agent_name", "agency_name", "is_house_agent", "dealer_number",
                        "dealer_name", "is_agent_of_record"], dropna=False)
             .agg(rep_start=("rep_start", "min"), first_sale=("first_entered_date", "min"),
                  last_sale=("last_entered_date", "max"), contracts=("contracts_written", "sum"))
             .reset_index().sort_values(["agent_code", "contracts"], ascending=[True, False]))
    moved = pairs[~pairs["is_agent_of_record"].astype(bool)]
    agencies = pairs.groupby("agency_name").agg(agents=("agent_key", "nunique"), stores=("dealer_number", "nunique"),
                                                contracts=("contracts", "sum")).reset_index() \
        .sort_values("contracts", ascending=False)
    kpis = [
        Kpi("Agents with sales", num(pairs["agent_key"].nunique()), "reps who brought in at least one store"),
        Kpi("Agent-store pairs", num(len(pairs)), "a store counts once for each rep who sold there"),
        Kpi("Stores now with another rep", num(moved["dealer_number"].nunique()),
            "a rep sold there before, but the store's current rep is someone else"),
    ]
    s1 = Section(
        "Stores by agent, since the rep started",
        "One row per agent and store: the rep's start date (their first sale anywhere), the first and last sale "
        "at that store and contracts since the start date. The Excel tab has every month to re-cut from any date.",
        cols=["Agent", "Agency", "Rep started", "Store", "First sale here", "Last sale here", "Contracts",
              "Still the store's rep?"],
        rows=[[f"{r.agent_code} {r.agent_name}", r.agency_name, f"{pd.Timestamp(r.rep_start):%b %Y}",
               f"{r.dealer_number} {r.dealer_name or ''}".strip(), f"{pd.Timestamp(r.first_sale):%Y-%m-%d}",
               f"{pd.Timestamp(r.last_sale):%Y-%m-%d}", num(r.contracts),
               {"text": "Yes" if r.is_agent_of_record else "No, reassigned",
                "status": "good" if r.is_agent_of_record else "warning"}]
              for r in pairs.head(60).itertuples()],
        wide=True, table_open=True)
    s2 = Section(
        "Agency roll-up",
        "Agents grouped under their agency. A house agent sells for the administrator directly.",
        cols=["Agency", "Agents", "Stores", "Contracts"],
        rows=[[r.agency_name, num(r.agents), num(r.stores), num(r.contracts)] for r in agencies.itertuples()],
        table_open=True)
    xlsx = write_workbook(out / DOWNLOADS / "agent_dealer_production.xlsx", {
        "Agent x store": pairs.drop(columns=["agent_key"]),
        "By month": a.drop(columns=["agent_key", "dealer_key", "rep_start"]).sort_values(
            ["agent_code", "dealer_number", "month_start"]),
    }, notes=["Every store an agent brought in. Filter the By month tab from any start date (synthetic data)."])
    return _page("dealer/agents.html", "Agents and their stores",
                 "Every store an agent brought in, since the rep started. Use it when a rep leaves, so the next "
                 "rep sees every store to take over.", kpis, [s1, s2], hub, crumb), xlsx


# --------------------------------------------------------------------------- incentive statement (edit 46)
SECTIONS = [("commissions", "commission_contracts", "New-sale commission"),
            ("cancels", "cancel_contracts", "Cancels (commission taken back)"),
            ("reinstatements", "reinstatement_contracts", "Reinstatements (commission given back)"),
            ("amendments", "amendment_contracts", "Amendments"),
            ("other_adjustments", "other_contracts", "Other adjustments")]


def page_incentive(con, out: Path, last_month: pd.Timestamp, hub: str, crumb: str) -> tuple[Page, Path] | None:
    s = _try(con, "select * exclude (_built_at) from dealer.mart_dealer_incentive_statement")
    if s is None:
        return None
    s["remit_received_date"] = pd.to_datetime(s["remit_received_date"])
    start = last_month - pd.DateOffset(months=11)
    end = last_month + pd.offsets.MonthEnd(0)
    t12 = s[(s["statement_status"] == "REMITTED") & s["remit_received_date"].between(start, end)]
    waiting = s[s["statement_status"].isin(["NOT_YET_REMITTED", "NEXT_REMITTANCE"])]  # LEGACY was paid before
    total = float(t12["total_due"].sum())
    kpis = [
        Kpi("Incentive due, T12", money(total), f"{num(t12['remit_batch_id'].nunique())} remittances received",
            raw=total),
        Kpi("New-sale commission, T12", money(t12["commissions"].sum()), "on contracts the store remitted"),
        Kpi("Cancels taken back, T12", money(t12["cancels"].sum()), "commission returned on cancelled contracts"),
        Kpi("Waiting for the next remittance", money(waiting["total_due"].sum()),
            "sales not remitted yet, and adjustments after the store's last remittance"),
    ]
    sec_rows = [[lbl, num(t12[cnt].sum()), money(t12[amt].sum())] for amt, cnt, lbl in SECTIONS]
    sec_rows.append(["Total due", "", {"text": money(total), "status": ""}])
    s1 = Section(
        "Incentive statement sections, T12",
        "The four detail sections of every statement plus other adjustments, last 12 months. Total due is the sum "
        "of the sections; a dbt test fails the build if it is not.",
        cols=["Section", "Contracts", "Amount"], rows=sec_rows, table_open=True)
    latest = (t12.groupby(["remit_batch_id", "remit_received_date", "dealer_number", "dealer_name"], dropna=False)
              [[a for a, _, _ in SECTIONS] + ["total_due"]].sum().reset_index()
              .sort_values("remit_received_date", ascending=False).head(40))
    s2 = Section(
        "Latest 40 statements",
        "One statement per remittance: what we owe the store for that payment, line by line.",
        cols=["Remittance", "Received", "Store", *[lbl.split(" (")[0] for _, _, lbl in SECTIONS], "Total due"],
        rows=[[r.remit_batch_id, f"{pd.Timestamp(r.remit_received_date):%Y-%m-%d}",
               f"{r.dealer_number} {r.dealer_name or ''}".strip(),
               *[money(getattr(r, a)) for a, _, _ in SECTIONS], money(r.total_due)] for r in latest.itertuples()],
        wide=True, table_open=True)
    xlsx = write_workbook(out / DOWNLOADS / "dealer_incentive_statement.xlsx", {
        "Statements": s.sort_values(["dealer_number", "remit_received_date", "remit_batch_id", "product_type"]),
    }, notes=["Dealer incentive statement per remittance x product type (synthetic data).",
              "Total due = new-sale commission + cancels + reinstatements + amendments + other adjustments."])
    return _page("dealer/incentive-statement.html", "Dealer incentive statement",
                 f"What we owe each store for each remittance, line by line, through {month_label(last_month)}.",
                 kpis, [s1, s2], hub, crumb), xlsx


# --------------------------------------------------------------------------- ancillary pricing (edit 48)
def page_ancillary(con, out: Path, hub: str, crumb: str) -> tuple[Page, Path] | None:
    ap = _try(con, "select * exclude (_built_at) from dealer.mart_ancillary_pricing")
    if ap is None:
        return None
    grand = ap[ap["row_type"] == "GRAND_TOTAL"]
    g = grand.iloc[0] if len(grand) else None
    kpis = [
        Kpi("Add-on contracts sold", num(g.contracts) if g is not None else "Not enough data yet", "all time"),
        Kpi("Average add-on price", money(g.avg_retail_price) if g is not None else "Not enough data yet",
            "what the customer paid"),
        Kpi("Add-on markup", pct(g.markup_pct) if g is not None and pd.notna(g.markup_pct) else "Not enough data yet",
            "price over dealer cost, all stores", raw=None if g is None else g.markup_pct),
    ]
    by_sub = ap[ap["row_type"] == "GROUP_SUBCATEGORY"].groupby("subcategory").agg(
        contracts=("contracts", "sum"), retail=("retail_total", "sum"), cost=("dealer_cost_total", "sum"),
        lo=("min_retail_price", "min"), hi=("max_retail_price", "max")).reset_index() \
        .sort_values("contracts", ascending=False)
    s1 = Section(
        "Add-on prices by product",
        "Average, lowest and highest price for each add-on, all stores. Markup is the price over what the store "
        "paid for the coverage.",
        cols=["Add-on", "Contracts", "Average price", "Lowest", "Highest", "Markup"],
        rows=[[r.subcategory, num(r.contracts), money(r.retail / r.contracts if r.contracts else None), money(r.lo),
               money(r.hi), pct((r.retail - r.cost) / r.cost) if r.cost else "Not enough data yet"]
              for r in by_sub.itertuples()],
        table_open=True)
    stores = ap[ap["row_type"] == "DEALER_TOTAL"].sort_values("contracts", ascending=False)
    s2 = Section(
        "Stores, all add-ons together",
        "Each store's \"(all products)\" subtotal row. The Excel tab has every store x add-on row and the group "
        "subtotals.",
        cols=["Dealer group", "Store", "Contracts", "Average price", "Dealer cost", "Markup", "Markup %"],
        rows=[[r.dealer_group, f"{r.dealer_number} {r.dealer_name or ''}".strip(), num(r.contracts),
               money(r.avg_retail_price), money(r.dealer_cost_total), money(r.markup_total),
               pct(r.markup_pct) if pd.notna(r.markup_pct) else "Not enough data yet"]
              for r in stores.head(40).itertuples()],
        wide=True, table_open=True)
    xlsx = write_workbook(out / DOWNLOADS / "ancillary_pricing.xlsx", {"Pricing": ap}, notes=[
        "Dealer group x store x add-on with subtotal rows: (all products) per store, (all dealers) per group, "
        "and a grand total (synthetic data)."])
    return _page("dealer/ancillary-pricing.html", "Ancillary pricing and markup",
                 "What each store charges for add-ons, and its markup over dealer cost.", kpis, [s1, s2], hub,
                 crumb), xlsx


# --------------------------------------------------------------------------- sections on existing pages
def activation_section(con) -> list[Section]:
    """Production page (edit 25): this month's pending vs active, and the three months before."""
    ac = _try(con, "select * exclude (_built_at) from dealer.mart_dealer_activations")
    if ac is None or ac.empty:
        return []
    m = ac.groupby(["month_start", "is_current_month", "product_family"])[["contracts_pending", "contracts_active"]] \
        .sum().reset_index().sort_values(["month_start", "product_family"], ascending=[False, True])
    return [Section(
        "Current month activations",
        "This month: what is still pending, what is active, by product family, with the three months before. The "
        "current month is still in progress, so its counts grow until it ends.",
        cols=["Month", "Product family", "Still pending", "Active"],
        rows=[[f"{pd.Timestamp(r.month_start):%b %Y}" + (" (in progress)" if r.is_current_month else ""),
               r.product_family, num(r.contracts_pending), num(r.contracts_active)] for r in m.itertuples()],
        table_open=True)]


def chassis_section(sp: pd.DataFrame) -> tuple[list[Kpi], list[Section]]:
    """Specialty page (edit 27): how each chassis VIN was typed, checked before anyone types it in."""
    if "chassis_vin_status" not in sp:
        return [], []
    rv = sp[sp["chassis_vin_status"].notna()]
    counts = rv["chassis_vin_status"].value_counts()
    bad = int(counts.drop(labels=["valid", "lowercase_fixed"], errors="ignore").sum())
    kpi = Kpi("RV chassis VINs needing a fix", num(bad) if len(rv) else "Not enough data yet",
              f"of {num(len(rv))} RV contracts", raw=bad / len(rv) if len(rv) else None)
    rows = [[VIN_STATUS[k][0], num(counts.get(k, 0)), pct(counts.get(k, 0) / len(rv)) if len(rv) else "n/a",
             VIN_STATUS[k][1]] for k in VIN_STATUS]
    return [kpi], [Section(
        "Chassis VIN check",
        "How the chassis number was typed on each RV contract. Only the shape is checked; the number itself "
        "stays hidden.",
        cols=["What was typed", "Contracts", "Share", "What to do"], rows=rows, table_open=True)]


def build(con, out: Path, last_month: pd.Timestamp, hub: str, crumb: str) -> list[tuple[Page, Path]]:
    built = [page_agents(con, out, hub, crumb), page_incentive(con, out, last_month, hub, crumb),
             page_ancillary(con, out, hub, crumb)]
    return [b for b in built if b is not None]
