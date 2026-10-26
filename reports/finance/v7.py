"""V7 finance pages and sections (docs/V3_DESIGN.md 10.4): the portfolio facts pack with the dealer-count
reconciliation (edits 34 + 42 + 43) and its Volume / Cancel / Methodology workbook, mileage-near-limit
cancels (36), cost-split totals by disbursement group on Close controls (21), reversed cancels on the
reserve bridge (23) and the commission as-of stamp with an explicit Unassigned row (28).

Reads the ``finance`` marts only, like the rest of the package. Every frame is optional: an older
warehouse without a V7 mart simply skips that page or section instead of failing the site build.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from reports.build import Kpi, Page, Section, link, money, num, pct
from reports.dealer import excel as dealer_excel  # the shared frozen-header workbook writer

PACK = "portfolio_facts.xlsx"
WINDOW_NAME = {"ITD": "Since the start (ITD)", "TTM": "Last 12 months (T12)"}
METHOD = {  # metric_id -> (label, how it is counted); the Methodology tab and the page share this text
    "ACTIVE_DEALERS": ("Active dealers", "Dealers with at least one contract in the window that was not voided "
                       "or denied."),
    "DEALER_GROUPS": ("Dealer groups", "Groups those active dealers belong to."),
    "CONTRACTS": ("Contracts", "Contracts entered in the window, leaving out voided and denied ones."),
    "OF_WHICH_BUNDLES": ("of which bundles", "Contracts whose product is a bundle of several add-ons."),
    "OF_WHICH_LIMITED_WARRANTY": ("of which limited warranty", "Contracts in the limited-warranty category."),
    "CANCEL_RATE_COHORT": ("Cancel rate, by sale group", "Contracts sold in the window that have cancelled since, "
                           "divided by contracts sold in the window."),
    "CANCEL_RATE_COHORT_EXCL_ADMIN": ("Cancel rate, by sale group, without admin corrections",
                                      "The same, leaving out cancels booked only to fix an admin error."),
    "CANCEL_RATE_FLOW": ("Cancel rate, by cancel date", "Cancels dated in the window, divided by contracts sold "
                         "in the window."),
    "CANCEL_RATE_FLOW_EXCL_ADMIN": ("Cancel rate, by cancel date, without admin corrections",
                                    "The same, leaving out admin-correction cancels."),
}
BASIS = {  # dealer-count basis -> plain label
    "TTM_NONVOIDED": "Sold in the last 12 months (the pack's definition)",
    "TTM_INCL_VOIDED": "Sold in the last 12 months, counting voided and denied contracts",
    "ITD_NONVOIDED": "Ever sold a contract that was not voided or denied",
    "ITD_ALL": "Ever sold any contract",
    "DEALER_DIM_TOTAL": "Every dealer on file, sold or not",
    "CROSS_TENANT_DEDUP": "Ever sold, a store that sells for two programs counted once",
}
LINE_NAME = {"VSC": "VSC", "GAP": "GAP", "TIRE_WHEEL": "Tire and wheel", "PREPAID_MAINTENANCE": "Prepaid maintenance",
             "ANCILLARY_OTHER": "Other add-ons", "UNMAPPED": "Not mapped yet"}
SHORT = {"CANCEL_RATE_COHORT": "By sale group", "CANCEL_RATE_COHORT_EXCL_ADMIN": "By sale group, no admin fixes",
         "CANCEL_RATE_FLOW": "By cancel date", "CANCEL_RATE_FLOW_EXCL_ADMIN": "By cancel date, no admin fixes"}
GROUP_COLS = [("group_admin", "Admin"), ("group_premium", "Premium"),
              ("group_dealer_participation", "Dealer participation"), ("group_producer", "Producer"),
              ("group_tax_and_fees", "Tax and fees"), ("group_unassigned", "No group (must be $0)")]


def _try(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> pd.DataFrame | None:
    """A V7 frame, or None when the warehouse predates the mart or column."""
    try:
        return con.execute(sql, params or []).df()
    except duckdb.Error:
        return None


def load(con: duckdb.DuckDBPyConnection, closed: int, t12: list[int]) -> dict[str, pd.DataFrame | None]:
    d: dict[str, pd.DataFrame | None] = {}
    d["facts"] = _try(con, "select * exclude (_built_at) from finance.mart_portfolio_facts "
                           "order by window_sort, tenant_label, metric_sort, product_line")
    d["dealer_counts"] = _try(con, "select * exclude (_built_at) from finance.mart_dealer_count_reconciliation "
                                   "order by basis_sort, tenant_label")
    d["mileage"] = _try(con, """
        select * exclude (_built_at) from finance.mart_cancel_mileage
        where posting_period_key between ? and ?""", t12)
    d["groups"] = _try(con, """
        select txn_type, sum(split_amount) as split_amount, sum(group_admin) as group_admin,
            sum(group_premium) as group_premium, sum(group_dealer_participation) as group_dealer_participation,
            sum(group_producer) as group_producer, sum(group_tax_and_fees) as group_tax_and_fees,
            sum(group_unassigned) as group_unassigned
        from finance.mart_disbursement_control_totals where posting_period_key between ? and ?
        group by 1 order by 1""", t12)
    d["reversals"] = _try(con, """
        select sum(contracts_cancel_reversed) as contracts, sum(reversed_cancel_reserve) as reserve
        from finance.mart_reserve_bridge""")
    d["stamp"] = _try(con, """
        select max(as_of_period_key) as as_of_period_key, max(data_as_of_ts) as data_as_of_ts,
            max(commission_period_label) filter (where posting_period_key = ?) as closed_label
        from finance.mart_commission_hierarchy""", [closed])
    d["unassigned"] = _try(con, """
        select level, product_type, sum(contracts) as contracts, sum(commission) as commission
        from finance.mart_commission_hierarchy
        where is_unassigned and posting_period_key between ? and ?
        group by 1, 2 order by 1, 2""", t12)
    return d


def _ok(d: dict, *names: str) -> bool:
    return all(isinstance(d.get(n), pd.DataFrame) for n in names)


def window_name(window_id: str, label: str) -> str:
    return WINDOW_NAME.get(window_id, label)


# --------------------------------------------------------------------------- portfolio facts (34 + 42 + 43)
def _fact(f: pd.DataFrame, window: str, metric: str, tenant: str = "COMBINED", line: str = "ALL") -> pd.Series | None:
    r = f[(f.window_id == window) & (f.metric_id == metric) & (f.tenant_scope == tenant) & (f.product_line == line)]
    return r.iloc[0] if len(r) else None


def _value(r: pd.Series | None) -> str:
    if r is None:
        return "Not enough data yet"
    if bool(r.is_rate):
        return pct(r.value) if pd.notna(r.value) else "Not enough data yet"
    return num(r.numerator)


def tenant_columns(f: pd.DataFrame) -> list[tuple[str, str]]:
    """(tenant_scope, label) with each tenant first and Combined last."""
    t = f.drop_duplicates("tenant_scope")[["tenant_scope", "tenant_label"]]
    t = t.assign(last=t.tenant_scope == "COMBINED").sort_values(["last", "tenant_label"])
    return list(t[["tenant_scope", "tenant_label"]].itertuples(index=False, name=None))


def page_portfolio_facts(d: dict) -> Page | None:
    if not _ok(d, "facts", "dealer_counts") or d["facts"].empty:
        return None
    f, dc = d["facts"], d["dealer_counts"]
    tenants = tenant_columns(f)
    as_of = pd.Timestamp(f.as_of_date.iloc[0])
    t12 = "TTM"
    kpis = [
        Kpi("Active dealers, T12", _value(_fact(f, t12, "ACTIVE_DEALERS")),
            "stores that sold at least one live contract in the last 12 months",
            raw=_fact(f, t12, "ACTIVE_DEALERS").numerator if _fact(f, t12, "ACTIVE_DEALERS") is not None else None),
        Kpi("Dealer groups, T12", _value(_fact(f, t12, "DEALER_GROUPS")), "groups those stores belong to"),
        Kpi("Contracts sold, T12", _value(_fact(f, t12, "CONTRACTS")), "sold in the last 12 months, all programs"),
        Kpi("Cancel rate by sale group, T12", _value(_fact(f, t12, "CANCEL_RATE_COHORT")),
            "share of last-12-month sales that cancelled since",
            raw=_fact(f, t12, "CANCEL_RATE_COHORT").value if _fact(f, t12, "CANCEL_RATE_COHORT") is not None else None),
    ]
    windows = f.drop_duplicates("window_id").sort_values("window_sort")
    cols = ["Window", *[lbl for _, lbl in tenants]]
    volume = Section(
        "Contracts by window",
        "Contracts sold in each window, one column per program and a Combined column. Voided and denied "
        "contracts never count.",
        cols=cols,
        rows=[[window_name(w.window_id, w.window_label),
               *[_value(_fact(f, w.window_id, "CONTRACTS", t)) for t, _ in tenants]] for w in windows.itertuples()],
        wide=True, table_open=True)
    ttm = f[(f.window_id == t12) & (f.metric_id.isin(["CONTRACTS", "OF_WHICH_BUNDLES", "OF_WHICH_LIMITED_WARRANTY"]))
            & (f.tenant_scope == "COMBINED")]
    lines_ = sorted(set(ttm.product_line) - {"ALL"}) + ["ALL"]
    by_line = Section(
        "Contracts by product line, T12",
        "Last 12 months, all programs together. Bundles and limited warranties are shown inside their product "
        "line, not as extra contracts.",
        cols=["Product line", "Contracts", "of which bundles", "of which limited warranty"],
        rows=[[("All product lines" if ln == "ALL" else LINE_NAME.get(ln, ln.replace("_", " ").title())),
               *[_value(_fact(f, t12, m, "COMBINED", ln))
                 for m in ("CONTRACTS", "OF_WHICH_BUNDLES", "OF_WHICH_LIMITED_WARRANTY")]] for ln in lines_],
        wide=True, table_open=True)
    rates = ["CANCEL_RATE_COHORT", "CANCEL_RATE_COHORT_EXCL_ADMIN", "CANCEL_RATE_FLOW", "CANCEL_RATE_FLOW_EXCL_ADMIN"]
    cancel = Section(
        "Cancel rate by method",
        "Two ways to count cancels, each with and without admin-correction cancels, all programs together. "
        "The method is written next to every number so two readers get the same answer.",
        cols=["Window", *[SHORT[m] for m in rates]],
        rows=[[window_name(w.window_id, w.window_label), *[_value(_fact(f, w.window_id, m)) for m in rates]]
              for w in windows.itertuples()],
        wide=True, table_open=True)
    dtenants = tenant_columns(dc)
    counts = Section(
        "Dealer counts side by side",
        f"Six ways to count dealers at {as_of:%b %d, %Y}. The first row is the definition this pack uses; the "
        "last column says how far each other way sits from it.",
        cols=["How dealers are counted", *[lbl for _, lbl in dtenants], "Combined vs the pack's count"],
        rows=[[BASIS.get(b, b),
               *[num(dc[(dc.basis == b) & (dc.tenant_scope == t)].dealer_count.sum()) for t, _ in dtenants],
               f"{int(dc[(dc.basis == b) & (dc.tenant_scope == 'COMBINED')].difference_vs_active.sum()):+,d}"]
              for b in dc.drop_duplicates("basis").sort_values("basis_sort").basis],
        wide=True, table_open=True)
    method = Section(
        "How each number is counted",
        "The Methodology tab of the workbook holds the same text.",
        cols=["Number", "How it is counted"], rows=[[lbl, how] for lbl, how in METHOD.values()], table_open=True)
    dl = Section("Download the pack", "Volume, Cancel, Dealer counts and Methodology tabs.",
                 cols=["Workbook"], rows=[[link(PACK, f"downloads/finance/{PACK}")]], table_open=True)
    return Page("finance/portfolio-facts.html", "Portfolio facts",
                f"How big the book is, defined once, through {as_of:%b %d, %Y} (the latest closed month). "
                "Each program has its own column, plus Combined.", kpis, [volume, by_line, cancel, counts, method, dl])


def pack_sheets(d: dict) -> dict[str, pd.DataFrame]:
    """Volume, Cancel, Dealer_Counts and Methodology tabs: tenant columns plus Combined, numbers only."""
    f, dc = d["facts"], d["dealer_counts"]
    names = dict(tenant_columns(f))

    def wide(metrics: list[str], field: str) -> pd.DataFrame:
        x = f[f.metric_id.isin(metrics)].copy()
        x["metric"] = x.metric_id.map(lambda m: METHOD[m][0])
        x["window"] = [window_name(a, b) for a, b in zip(x.window_id, x.window_label, strict=True)]
        p = x.pivot_table(index=["window_sort", "window", "metric_sort", "metric", "product_line"],
                          columns="tenant_scope", values=field, aggfunc="sum").reset_index()
        p = p.rename(columns=names)
        order = [lbl for _, lbl in tenant_columns(f) if lbl in p.columns]
        return p.sort_values(["window_sort", "metric_sort", "product_line"])[
            ["window", "metric", "product_line", *order]]

    volume = wide(["ACTIVE_DEALERS", "DEALER_GROUPS", "CONTRACTS", "OF_WHICH_BUNDLES", "OF_WHICH_LIMITED_WARRANTY"],
                  "numerator")
    cancel = wide([m for m in METHOD if m.startswith("CANCEL_RATE")], "value")
    counts = dc.assign(basis_label=dc.basis.map(BASIS)).pivot_table(
        index=["basis_sort", "basis_label"], columns="tenant_label", values="dealer_count", aggfunc="sum").reset_index()
    as_of = pd.Timestamp(f.as_of_date.iloc[0])
    methodology = pd.DataFrame(
        [("As of", f"{as_of:%Y-%m-%d}, the last day of the latest closed accounting month.")]
        + [("Windows", "ITD = since the start; T12 = last 12 months; YTD = this calendar year so far; CY = one "
            "full earlier calendar year.")]
        + [(lbl, how) for lbl, how in METHOD.values()]
        + [(BASIS[b], "Dealer-count basis; see the Dealer_Counts tab.") for b in BASIS]
        + [("Data", "Synthetic data generated by the pipeline.")], columns=["item", "how it is counted"])
    return {"Volume": volume, "Cancel": cancel, "Dealer_Counts": counts.drop(columns="basis_sort"),
            "Methodology": methodology}


def write_pack(d: dict, out_dir: Path) -> Path | None:
    if not _ok(d, "facts", "dealer_counts") or d["facts"].empty:
        return None
    return dealer_excel.write_workbook(Path(out_dir) / "downloads" / "finance" / PACK, pack_sheets(d))


# --------------------------------------------------------------------------- mileage-near-limit cancels (36)
def page_cancel_mileage(d: dict, span: str) -> Page | None:
    if not _ok(d, "mileage"):
        return None
    m = d["mileage"]
    banded = m[m.mileage_band_id.notna()]
    near = banded[banded.is_near_limit.astype(bool)]
    over = banded[banded.mileage_band_id == "OVER"]
    kpis = [
        Kpi("Cancels with a mileage limit, T12", num(len(m)), f"cancelled {span}"),
        Kpi("Cancelled near the mileage limit, T12", num(len(near)) if len(m) else "Not enough data yet",
            f"{pct(len(near) / len(banded)) if len(banded) else 'no odometer readings'} of those with a reading",
            raw=len(near) / len(banded) if len(banded) else None),
        Kpi("Cancelled past the mileage limit, T12", num(len(over)) if len(m) else "Not enough data yet",
            "odometer already past the limit at cancel"),
    ]
    bands = (banded.groupby(["mileage_band_sort", "mileage_band", "is_near_limit"])
             .agg(cancels=("cancel_key", "count"), refunds=("customer_refund", "sum")).reset_index())
    missing = int(m.mileage_band_id.isna().sum())
    s1 = Section(
        "Cancels by miles left",
        "Miles left = the contract's mileage limit minus the miles driven since the sale. A cancel with no "
        "odometer reading is counted on its own line, never as zero miles.",
        cols=["Miles left at cancel", "Near the limit?", "Cancels", "Customer refunds"],
        rows=[[r.mileage_band, "Yes" if r.is_near_limit else "No", num(r.cancels), money(r.refunds)]
              for r in bands.itertuples()] + ([["No odometer reading", "n/a", num(missing), "n/a"]] if missing else []),
        table_open=True)
    return Page("finance/cancel-mileage.html", "Cancels near the mileage limit",
                f"Contracts cancelled close to their mileage limit, {span}. Bands come from seed "
                "cancel_mileage_bands.", kpis, [s1])


# --------------------------------------------------------------------------- sections on existing pages
def group_section(d: dict, money_cents, span: str) -> list[Section]:
    """Close controls (edit 21): the same cost split by the group the program setup put each line in."""
    if not _ok(d, "groups") or d["groups"].empty:
        return []
    g = d["groups"]
    rows = []
    for col, lbl in GROUP_COLS:
        rows.append([lbl, *[money_cents(v) for v in g[col]], money_cents(g[col].sum())])
    total = g[[c for c, _ in GROUP_COLS]].sum(axis=1)
    diff = float((total - g["split_amount"]).abs().max())
    rows.append(["All groups", *[money_cents(v) for v in total], money_cents(total.sum())])
    rows.append(["Cost split total", *[money_cents(v) for v in g["split_amount"]],
                 {"text": money_cents(g["split_amount"].sum()), "status": "good" if diff < 0.05 else "critical"}])
    return [Section(f"Cost split by disbursement group, T12 ({span})",
                    "The same cost split as above, grouped by the bucket each line was booked to. The program setup "
                    "picks the bucket, so one fee type can sit in different groups. The groups must add back to the "
                    "cost split total.",
                    cols=["Group", *[str(t).title() for t in g["txn_type"]], "Total"], rows=rows, wide=True,
                    table_open=True)]


def reversal_rows(d: dict, money_cents) -> list[list]:
    """Reserve bridge (edit 23): reversed cancels, shown even when there are none."""
    if not _ok(d, "reversals") or d["reversals"].empty:
        return []
    r = d["reversals"].iloc[0]
    n = 0 if pd.isna(r.contracts) else int(r.contracts)
    v = 0.0 if pd.isna(r.reserve) else float(r.reserve)
    return [["Cancels the source later reversed (inside other adjustments; the pair nets to $0.00)",
             money_cents(v), num(n)]]


def commission_stamp(d: dict) -> tuple[str, list[Section]]:
    """Commission hierarchy (edit 28): the header stamp and the explicit Unassigned row."""
    if not _ok(d, "stamp") or d["stamp"].empty:
        return "", []
    s = d["stamp"].iloc[0]
    ts = pd.Timestamp(s.data_as_of_ts) if pd.notna(s.data_as_of_ts) else None
    label = s.closed_label if isinstance(s.closed_label, str) else "n/a"
    stamp = f" Commission period {label}, data as of {ts:%b %d, %Y %H:%M}." if ts is not None else ""
    un = d.get("unassigned")
    rows = [[r.level.title(), r.product_type, num(r.contracts), money(r.commission)] for r in un.itertuples()] \
        if isinstance(un, pd.DataFrame) and len(un) else [["All levels", "All", "0", money(0)]]
    return stamp, [Section(
        "Unassigned commission",
        "Contracts and commission with no district or manager. They sit in this row on purpose, counted by a "
        "safety check, so nothing goes missing from a total.",
        cols=["Level", "Product type", "Contracts", "Commission"], rows=rows, table_open=True)]


def index_rows(d: dict) -> list[tuple[str, str, str]]:
    rows = []
    if _ok(d, "facts", "dealer_counts") and not d["facts"].empty:
        rows.append(("finance/portfolio-facts.html", "Portfolio facts",
                     "Active dealers, volume and cancel rates by window, with the method next to each number"))
    if _ok(d, "mileage"):
        rows.append(("finance/cancel-mileage.html", "Cancels near the mileage limit",
                     "Contracts cancelled close to their mileage limit"))
    return rows


def pages(d: dict, span: str) -> list[Page]:
    return [p for p in (page_portfolio_facts(d), page_cancel_mileage(d, span)) if p is not None]
