"""Finance report pages: cession / experience, profit-share statement, commission statement,
P&L by product, remittance aging with dealer late fees, and the cost-split close controls; plus the
V6 pages in ``reports.finance.v6`` (Experience, New Reserves, commission hierarchy).

Plug-in for the report shell (docs/V3_DESIGN.md section 3): ``build(con, out_dir)`` returns the
pages and writes the Excel pack to ``site/downloads/finance/``. Reads the ``finance`` marts and the
``seeds`` finance inputs only. Statements use the latest CLOSED posting period, so a figure never
moves after it is published; trailing-12 views end at that period too.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from reports import charts
from reports.build import Kpi, Page, Section, chart_section, link, money, num, pct
from reports.finance import v6, v7

AREA = "finance"
NAV_LABEL = "Finance"  # the shell links the nav entry to the first page returned (finance/index.html)
WORKBOOK = "finance_pack.xlsx"
SEEDS = ["participation_structure_terms", "pnl_line_map", "remit_aging_buckets", "control_tolerances",
         "term_year_buckets", "commission_volume_targets"]
STRUCTURE_LABEL = {
    "DEALER_OWNED_OBLIGOR": "Dealer-owned obligor", "CFC": "CFC", "NCFC": "NCFC", "PORC": "PORC",
    "RETRO": "Retro", "PROFIT_SHARE": "Profit share",
}


# --------------------------------------------------------------------------- formatting
def money_cents(v: float | None) -> str:
    """Exact dollars and cents for control totals, sign before the symbol: -$1,082.15 (never $-1,082.15)."""
    if v is None or pd.isna(v):
        return "n/a"
    body = f"{abs(v):,.2f}"
    return f"{'-' if v < 0 and body != '0.00' else ''}${body}"


# --------------------------------------------------------------------------- data
def period_label(key: int | float | None) -> str:
    if key is None or pd.isna(key):
        return "n/a"
    k = int(key)
    return pd.Timestamp(year=k // 100, month=k % 100, day=1).strftime("%b %Y")


def period_start(key: int) -> pd.Timestamp:
    return pd.Timestamp(year=int(key) // 100, month=int(key) % 100, day=1)


def shift_period(key: int, months: int) -> int:
    ts = period_start(key) + pd.DateOffset(months=months)
    return ts.year * 100 + ts.month


def _df(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> pd.DataFrame:
    return con.execute(sql, params or []).df()


def load(con: duckdb.DuckDBPyConnection) -> dict[str, pd.DataFrame | int]:
    """Pull every frame the pages need (one query per mart, already at report grain)."""
    closed = con.execute(
        "select max(posting_period_key) from finance.mart_cession_experience where is_closed").fetchone()[0]
    if closed is None:
        closed = con.execute("select max(posting_period_key) from finance.mart_cession_experience").fetchone()[0]
    start = shift_period(closed, -11)
    t12 = [start, closed]
    d: dict[str, pd.DataFrame | int] = {"closed": closed, "t12_start": start}
    d["cession"] = _df(con, """
        select * exclude (_built_at) from finance.mart_cession_experience
        where posting_period_key = ? order by entity_type, entity_name""", [closed])
    d["cession_monthly"] = _df(con, """
        select posting_period_key, sum(earned_reserve) as earned_reserve, sum(incurred_claims) as incurred_claims
        from finance.mart_cession_experience where posting_period_key between ? and ?
        group by 1 order by 1""", [shift_period(closed, -23), closed])
    d["statement"] = _df(con, """
        select * exclude (_built_at) from finance.mart_profit_share_statement
        where posting_period_key = ? order by closing_profit_share_due desc""", [closed])
    d["ps_monthly"] = _df(con, """
        select posting_period_key, sum(period_profit_share) as period_profit_share,
            sum(distributions_paid) as distributions_paid,
            coalesce(sum(greatest(closing_profit_share_due, 0)) filter (where due_category = 'PAYABLE'), 0)
                as closing_payable,
            coalesce(sum(least(closing_profit_share_due, 0)) filter (where due_category = 'PAYABLE'), 0)
                as closing_recoverable,
            coalesce(sum(greatest(closing_profit_share_due, 0)) filter (where due_category <> 'PAYABLE'), 0)
                as closing_surplus,
            coalesce(sum(least(closing_profit_share_due, 0)) filter (where due_category <> 'PAYABLE'), 0)
                as closing_over_released
        from finance.mart_profit_share_statement where posting_period_key between ? and ?
        group by 1 order by 1""", [shift_period(closed, -23), closed])
    d["commission"] = _df(con, """
        select agent_key, bool_or(is_no_agent_ledger) as is_no_agent_ledger,
            any_value(agent_code) as agent_code, any_value(agent_name) as agent_name,
            any_value(territory) as territory, sum(contracts_first_commissioned) as contracts,
            sum(contracts_first_commissioned_primary) as contracts_distinct,
            sum(commission_written) as written, sum(commission_expensed) as expensed,
            sum(commission_chargebacks) as chargebacks,
            sum(advances_paid) as advances, sum(advance_recoveries) as recoveries,
            sum(advances_net) as advances_net, sum(net_commission) as net,
            sum(commission_earned_on_curve) as on_curve, sum(expensed_ahead_of_curve) as ahead_of_curve,
            sum(arg_max_balance) as advance_balance
        from (select *, case when posting_period_key = max(posting_period_key) over (partition by agent_key,
                tenant_key, source_system) then advance_balance else 0 end as arg_max_balance
            from finance.mart_commission_statement where posting_period_key between ? and ?)
        group by 1 order by is_no_agent_ledger, net desc""", t12)
    d["commission_itd"] = _df(con, """
        select source_system, bool_or(is_no_agent_ledger) as is_no_agent_ledger,
            sum(commission_written) as written, sum(commission_chargebacks) as chargebacks,
            sum(advances_paid - advance_recoveries) as advances_net, sum(net_commission) as net,
            sum(commission_lines) as lines
        from finance.mart_commission_statement where posting_period_key <= ?
        group by 1 order by 2, 1""", [closed])
    d["commission_monthly"] = _df(con, """
        select posting_period_key, sum(commission_expensed) as expensed, sum(commission_chargebacks) as chargebacks,
            sum(advances_net) as advances_net, sum(net_commission) as net
        from finance.mart_commission_statement where posting_period_key between ? and ?
        group by 1 order by 1""", [shift_period(closed, -23), closed])
    d["pnl"] = _df(con, """
        select is_unmatched_claims, category, subcategory, product_code,
            sum(contracts_written) as contracts_written,
            sum(written_coverage_cost) as written, sum(coverage_cost_refunded) as refunded,
            sum(coverage_cost_reinstated) as reinstated, sum(net_written_coverage_cost) as net_written,
            sum(earned_premium) as earned_premium, sum(earned_claims_reserve) as earned_reserve,
            sum(earned_admin_fee) as admin_fee, sum(earned_acquisition_cost) as acquisition,
            sum(earned_commission) as earned_commission,
            sum(earned_profit_share_reserve) as profit_share_reserve,
            sum(claim_lines) as claim_lines, sum(incurred_claims) as incurred,
            sum(program_margin) as program_margin, sum(dealer_participation) as participation,
            sum(program_margin_after_participation) as margin_after
        from finance.mart_pnl_by_product where posting_period_key between ? and ?
        group by all order by earned_premium desc""", t12)
    d["remit_buckets"] = _df(con, """
        select aging_bucket, min(aging_sort) as sort, sum(receipts) as receipts,
            sum(remitted_amount) as amount, sum(late_fee_billed) as late_fee
        from finance.mart_remittance_late_fees where posting_period_key between ? and ?
        group by 1 order by sort""", t12)
    d["remit_dealers"] = _df(con, """
        select dealer_number, any_value(dealer_name) as dealer_name, sum(receipts) as receipts,
            sum(late_receipts) as late_receipts, sum(remitted_amount) as amount,
            sum(late_fee_billed) as late_fee,
            sum(avg_remit_age_days * receipts) / nullif(sum(receipts), 0) as avg_age
        from finance.mart_remittance_late_fees where posting_period_key between ? and ?
        group by 1 order by late_fee desc, dealer_number""", t12)
    d["remit_monthly"] = _df(con, """
        select posting_period_key, sum(late_fee_billed) as late_fee, sum(late_receipts) as late_receipts,
            sum(receipts) as receipts
        from finance.mart_remittance_late_fees where posting_period_key between ? and ?
        group by 1 order by 1""", [shift_period(closed, -23), closed])
    d["controls"] = _df(con, """
        select * exclude (_built_at) from finance.mart_disbursement_control_totals
        order by posting_period_key desc, source_system, txn_type""")
    d["rollforward"] = _df(con, """
        select posting_period_key, sum(opening_funded_reserve) as opening, sum(net_reserve_written) as written,
            sum(claims_paid) as paid, sum(surplus_released) as released,
            sum(closing_funded_reserve) as closing,
            sum(reserve_earned) as earned, sum(closing_unearned_reserve) as unearned
        from finance.mart_reserve_rollforward where posting_period_key between ? and ?
        group by 1 order by 1 desc""", t12)
    d["reserve_recon"] = _df(con, """
        select * exclude (_built_at) from finance.mart_funded_reserve_reconciliation
        where posting_period_key = ? order by tenant_key""", [closed])
    for seed in SEEDS:
        d[f"seed_{seed}"] = _df(con, f"select * from seeds.{seed}")
    d.update(v6.load(con, closed, t12))
    d.update(v7.load(con, closed, t12))
    return d


def ratio(n: float, den: float) -> float | None:
    return None if not den or pd.isna(den) else n / den


def lr_status(v: float | None) -> str:
    if v is None or pd.isna(v):
        return ""
    return "critical" if v > 0.80 else ("warning" if v > 0.70 else "good")


def _monthly(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["month"] = out["posting_period_key"].map(period_start)
    return out


# --------------------------------------------------------------------------- pages
def page_index(d: dict) -> Page:
    ce, st, pnl, cm = d["cession"], d["statement"], d["pnl"], d["commission"]
    rb, ctl = d["remit_buckets"], d["controls"]
    lr = ratio(ce["itd_incurred_claims"].sum(), ce["itd_earned_reserve"].sum())
    pnl_lr = ratio(pnl["incurred"].sum(), pnl["earned_reserve"].sum())
    pay_due = st.loc[st["due_category"] == "PAYABLE", "closing_profit_share_due"]
    payable, recoverable = pay_due.clip(lower=0).sum(), pay_due.clip(upper=0).sum()
    surplus = st.loc[st["due_category"] != "PAYABLE", "closing_profit_share_due"].clip(lower=0).sum()
    bad = int((~ctl["is_balanced"]).sum())
    kpis = [
        Kpi("Participation earned reserve, ITD", money(ce["itd_earned_reserve"].sum()),
            f"{len(ce)} participation entities"),
        Kpi("Participation loss ratio, ITD", pct(lr), "incurred / earned reserve", lr_status(lr)),
        Kpi("Profit share payable to dealers", money(payable),
            f"owed on obligor-held programs at {period_label(d['closed'])}; "
            f"{money(-recoverable)} overpaid, recoverable (not netted)"),
        Kpi("Surplus in dealer-owned entities", money(surplus), "held in the dealer's own reinsurer or obligor"),
        Kpi("Earned premium, T12", money(pnl["earned_premium"].sum()), "all cost-split buckets"),
        Kpi("Posted loss ratio, T12", pct(pnl_lr), "incurred / earned claims reserve", lr_status(pnl_lr)),
        Kpi("Net commission, T12", money(cm["net"].sum()),
            f"{int((~cm['is_no_agent_ledger'].astype(bool)).sum())} agents, plus legacy contracts with no agent "
            "ledger"),
        Kpi("Late fees billed, T12", money(rb["late_fee"].sum()), "dealer remittance late fees"),
        Kpi("Cost-split controls", "Balanced" if bad == 0 else f"{bad} breaks",
            f"{len(ctl)} period lines checked", "good" if bad == 0 else "critical"),
    ]
    reports = [
        ("finance/cession.html", "Cession / experience",
         "Earned reserve, claims, loss ratio and reserve balance per participation entity"),
        ("finance/profit-share.html", "Profit-share statement",
         "Opening due, period movement and closing due per entity"),
        ("finance/commissions.html", "Commission statement",
         "Earned, chargebacks, advances and net commission per agent"),
        ("finance/pnl.html", "P&L by product", "Written to earned to margin, per product"),
        ("finance/remittance.html", "Remittance aging and late fees",
         "How fast dealers remit, and the late fees billed"),
        ("finance/controls.html", "Close controls",
         "Cost-split control totals, the reserve roll-forward and the reserve bridge"),
        *v6.index_rows(),
        *v7.index_rows(d),
        (f"downloads/{AREA}/{WORKBOOK}", "Finance pack (Excel)", "Inputs, then outputs, one tab per statement"),
    ]
    s = Section("Finance reports", "Every statement here is built from the posted ledger by the pipeline, "
                "checked by dbt tests that tie it back to the source facts, and refreshed every run.",
                cols=["Report", "What it shows"], rows=[[link(t, h), w] for h, t, w in reports], wide=True,
                table_open=True)
    return Page("finance/index.html", "Finance",
                f"Statements for the period ending {period_label(d['closed'])} (the latest closed period). "
                "Closed periods are final and never restated.", kpis, [s])


def page_cession(d: dict) -> Page:
    ce = d["cession"]
    by_type = ce.groupby("entity_type", as_index=False)[["itd_earned_reserve", "itd_incurred_claims"]].sum()
    by_type["label"] = by_type["entity_type"].map(STRUCTURE_LABEL)
    by_type["lr"] = by_type["itd_incurred_claims"] / by_type["itd_earned_reserve"]
    s1 = chart_section(
        "Earned reserve vs incurred claims by structure, ITD",
        "For each kind of dealer participation, the reserve earned so far against the claims it has paid "
        "or expects to pay. The gap is the underwriting result the dealer can share in.",
        charts.bars(by_type["label"], {"Earned reserve": by_type["itd_earned_reserve"],
                                       "Incurred claims": by_type["itd_incurred_claims"]},
                    yfmt="$~s", hover="$,.0f"),
        "fin-cession-type", ["Structure", "Earned reserve", "Incurred", "Loss ratio"],
        [[r.label, money(r.itd_earned_reserve), money(r.itd_incurred_claims), pct(r.lr)]
         for r in by_type.itertuples()],
    )
    m = _monthly(d["cession_monthly"])
    s2 = chart_section(
        "Monthly earned reserve and incurred claims, all entities",
        "The same two lines month by month for the last 24 posting periods.",
        charts.lines(m, "month", {"earned_reserve": "Earned reserve", "incurred_claims": "Incurred claims"},
                     yfmt="$~s", hover="$,.0f"),
        "fin-cession-monthly", ["Period", "Earned reserve", "Incurred"],
        [[period_label(r.posting_period_key), money(r.earned_reserve), money(r.incurred_claims)]
         for r in m.itertuples()][::-1],
    )
    rows = [[r.entity_name, STRUCTURE_LABEL.get(r.entity_type, r.entity_type), num(r.itd_contracts_written),
             money(r.itd_written_reserve), money(r.itd_earned_reserve), money(r.itd_incurred_claims),
             {"text": pct(r.itd_loss_ratio), "status": lr_status(r.itd_loss_ratio)},
             money(r.reserve_balance), money(r.unearned_reserve), money(r.itd_underwriting_result),
             money(r.itd_profit_share_due), money(r.itd_distributions_paid), money(r.closing_profit_share_due)]
            for r in ce.itertuples()]
    s3 = Section("Experience by participation entity, ITD",
                 "Reserve balance = claims reserve written (net of cancels) less claims paid less the surplus "
                 "released to the dealer by profit-share distributions (the part not covered by the "
                 "profit-share reserve). Unearned = written less earned. Loss ratio uses earned reserve, never "
                 "written. Profit share earned ITD is gross; closing due = earned ITD - paid out ITD, the same "
                 "figure as the Profit-share statement.",
                 cols=["Entity", "Structure", "Contracts", "Reserve written", "Earned", "Incurred", "Loss ratio",
                       "Reserve balance", "Unearned", "UW result", "Profit share earned, ITD", "Paid out, ITD",
                       "Closing due"],
                 rows=rows, wide=True, table_open=True)
    lr = ratio(ce["itd_incurred_claims"].sum(), ce["itd_earned_reserve"].sum())
    kpis = [
        Kpi("Reserve written, ITD", money(ce["itd_written_reserve"].sum())),
        Kpi("Earned reserve, ITD", money(ce["itd_earned_reserve"].sum())),
        Kpi("Incurred claims, ITD", money(ce["itd_incurred_claims"].sum())),
        Kpi("Loss ratio, ITD", pct(lr), "incurred / earned", lr_status(lr)),
        Kpi(f"Participation reserve balance ({len(ce)} entities)", money(ce["reserve_balance"].sum()),
            "written, less claims paid and surplus distributed; the book-wide roll-forward is on Close controls"),
        Kpi("Entities above 80% LR", str(int((ce["itd_loss_ratio"] > 0.80).sum())), f"of {len(ce)}"),
    ]
    return Page("finance/cession.html", "Cession / experience",
                f"Inception-to-date experience of every dealer participation entity at {period_label(d['closed'])}.",
                kpis, [s1, s2, s3, *v6.runoff_sections(d, STRUCTURE_LABEL)])


def page_profit_share(d: dict) -> Page:
    st = d["statement"]
    m = _monthly(d["ps_monthly"])
    s1 = chart_section(
        "Profit share movement by month",
        "How much the profit share earned by dealers went up (or down) each posting period, and what was "
        "paid out. It goes down when late claims eat into an entity's underwriting result. Once a year (seed "
        "profit_share_payout_schedule) the share earned to the prior December is paid out.",
        charts.bars(m["month"], {"Movement": m["period_profit_share"], "Paid out": -m["distributions_paid"]},
                    yfmt="$~s", hover="$,.0f"),
        "fin-ps-monthly", ["Period", "Movement", "Paid out", "Closing payable", "Overpaid, recoverable",
                           "Closing surplus (dealer-owned)", "Over-released"],
        [[period_label(r.posting_period_key), money(r.period_profit_share), money(r.distributions_paid),
          money(r.closing_payable), money(-r.closing_recoverable), money(r.closing_surplus),
          money(-r.closing_over_released)]
         for r in m.itertuples()][::-1], wide=True,
        note="Closing payable and surplus count only positive balances; overpaid and over-released balances are "
             "shown in their own columns and never netted, so the latest row equals the KPI cards above.",
    )
    rows = [[r.entity_name, STRUCTURE_LABEL.get(r.entity_type, r.entity_type), r.statement_status,
             money(r.itd_earned_reserve), money(r.itd_incurred_claims), money(r.itd_ceding_fee),
             money(r.itd_underwriting_result), pct(r.dealer_share_pct, 0),
             "yes" if r.is_seasoned else f"no ({int(r.months_seasoned)} mo)",
             money(r.opening_profit_share_due), money(r.period_profit_share), money(r.distributions_paid),
             {"text": money(r.closing_profit_share_due),
              "status": "warning" if r.closing_profit_share_due < -0.005 else ""},
             due_flag(r),
             money(r.funding_gap) if pd.notna(r.funding_gap) else "n/a"] for r in st.itertuples()]
    s2 = Section("Statement by entity",
                 "Earned ITD = dealer share x the positive ITD underwriting result (earned reserve - incurred - "
                 "ceding fee), once the entity is seasoned. Closing due = earned ITD - paid out ITD. For "
                 "dealer-owned structures (CFC, NCFC, PORC, retro, dealer-owned obligor) this is surplus already "
                 "held in the dealer's own entity, not a payable. Funding gap (profit-share structures only) = "
                 "share earned - (profit-share reserve + earned surplus left in the claims reserve); "
                 "negative = covered. A negative closing due means the December paid-through amount was "
                 "distributed and later experience got worse: the dealer was overpaid and the amount is "
                 "recoverable (Flag column); it is never netted against what other dealers are owed.",
                 cols=["Entity", "Structure", "Status", "Earned reserve", "Incurred", "Ceding fee", "UW result",
                       "Share", "Seasoned", "Opening due", "Movement", "Paid out", "Closing due", "Flag",
                       "Funding gap"],
                 rows=rows, wide=True, table_open=True)
    pay = st[st["due_category"] == "PAYABLE"]
    own = st[st["due_category"] != "PAYABLE"]
    pay_due, own_due = pay["closing_profit_share_due"], own["closing_profit_share_due"]
    check_ps_monthly_ties(d["ps_monthly"], st, d["closed"])
    kpis = [
        Kpi("Payable to dealers", money(pay_due.clip(lower=0).sum()),
            f"{int((pay_due > 0).sum())} of {len(pay)} profit-share programs; the obligor owes this"),
        Kpi("Overpaid, recoverable", money(-pay_due.clip(upper=0).sum()),
            f"{int((pay_due < 0).sum())} profit-share programs were paid more than they have earned since",
            "warning" if (pay_due < -0.005).any() else ""),
        Kpi("Underwriting surplus in dealer-owned entities", money(own_due.clip(lower=0).sum()),
            f"{len(own)} entities; already held by the dealer's reinsurer or obligor; "
            f"{money(-own_due.clip(upper=0).sum())} over-released in {int((own_due < 0).sum())} entities"),
        Kpi("Movement this period", money(st["period_profit_share"].sum())),
        Kpi("Paid out, last 12 months", money(d["ps_monthly"].loc[
            d["ps_monthly"]["posting_period_key"] >= d["t12_start"], "distributions_paid"].sum())),
        Kpi("Entities not yet seasoned", str(int((~st["is_seasoned"]).sum())), f"of {len(st)}"),
        Kpi("Profit-share funding gap", money(pay["funding_gap"].fillna(0).clip(lower=0).sum()),
            f"profit-share reserve alone is short {money(pay['ps_reserve_shortfall'].fillna(0).clip(lower=0).sum())};"
            " the earned claims-reserve surplus covers the rest"),
    ]
    return Page("finance/profit-share.html", "Profit-share statement",
                f"Statement for the posting period {period_label(d['closed'])}.", kpis, [s1, s2])


def check_ps_monthly_ties(ps: pd.DataFrame, st: pd.DataFrame, closed: int) -> None:
    """The monthly table's statement-period row must equal the statement KPIs (no netting)."""
    row = ps[ps["posting_period_key"] == closed]
    if row.empty:
        return
    pay = st.loc[st["due_category"] == "PAYABLE", "closing_profit_share_due"]
    own = st.loc[st["due_category"] != "PAYABLE", "closing_profit_share_due"]
    expected = {"closing_payable": pay.clip(lower=0).sum(), "closing_recoverable": pay.clip(upper=0).sum(),
                "closing_surplus": own.clip(lower=0).sum(), "closing_over_released": own.clip(upper=0).sum()}
    for col, want in expected.items():
        got = float(row[col].iloc[0])
        if abs(got - want) > 0.01:
            raise ValueError(f"profit-share monthly {col} {got:,.2f} at {closed} != statement {want:,.2f}")


def due_flag(r) -> str:
    """Flag for the statement table: a negative closing due is a recoverable, not a payable."""
    if r.closing_profit_share_due >= -0.005:
        return ""
    return "Overpaid, recoverable" if r.due_category == "PAYABLE" else "Over-released surplus"


def page_commissions(d: dict) -> Page:
    allc = d["commission"]
    is_nl = allc["is_no_agent_ledger"].astype(bool)
    cm, nl = allc[~is_nl], allc[is_nl]
    m = _monthly(d["commission_monthly"])
    s1 = chart_section(
        "Commission expensed and charged back, by month",
        "Commission expensed is on a cash basis: a standard-plan commission is expensed when it is paid at sale; "
        "an advance is expensed as the contract earns it down (the rest when it cancels). Chargebacks are shown "
        "as negative amounts on every finance page.",
        charts.lines(m, "month", {"expensed": "Expensed", "chargebacks": "Chargebacks"}, yfmt="$~s",
                     hover="$,.0f"),
        "fin-comm-monthly", ["Period", "Expensed (cash basis)", "Chargebacks", "Advances, net", "Net"],
        [[period_label(r.posting_period_key), money(r.expensed), money(r.chargebacks), money(r.advances_net),
          money(r.net)] for r in m.itertuples()][::-1], wide=True,
    )

    def agent_row(r) -> list:
        return [r.agent_code, r.agent_name, r.territory, num(r.contracts), money(r.written), money(r.expensed),
                money(r.chargebacks), money(r.advances), money(-r.recoveries), money(r.net),
                money(r.advance_balance), pct(ratio(-r.chargebacks, r.written))]

    rows = [agent_row(r) for r in cm.itertuples()]
    tot = cm[["contracts_distinct", "written", "expensed", "chargebacks", "advances", "recoveries", "net",
              "advance_balance"]].sum()
    # Totals count each contract once (its primary agent), so a contract split between agents is not
    # counted twice; agent rows count it for each agent.
    tot_row = lambda label, t: [label, "", "", num(t["contracts_distinct"]), money(t["written"]),  # noqa: E731
                                money(t["expensed"]), money(t["chargebacks"]), money(t["advances"]),
                                money(-t["recoveries"]), money(t["net"]), money(t["advance_balance"]),
                                pct(ratio(-t["chargebacks"], t["written"]))]
    rows.append(tot_row("Total, agent ledger", tot))
    if len(nl):
        rows += [agent_row(r) for r in nl.itertuples()]
        rows.append(tot_row("Total, cost-split commission", allc[tot.index].sum()))
    s2 = Section("Statement by agent, trailing 12 months",
                 "Written = commission paid at sale + advances paid. Net (cash) = expensed + chargebacks + advances "
                 "paid - advance earned down. Advance-plan agents are paid the commission up front; it is earned "
                 "down on the contract's own earning curve and that earning is booked as a recovery of the "
                 "advance (the rest is released when the contract cancels). The advance balance is the advance not "
                 "yet earned. Chargeback rate = chargebacks / commission written, the same base for both plans. "
                 "Legacy contracts were never fed to an agent ledger: their cost-split commission is one "
                 "reconciling line, so the total ties to the cost split (dbt test fin_commission_ties_to_cost_split). "
                 "Contracts commissioned (distinct) counts each contract once, in the period of its first "
                 "commission line (an amended, reinstated or re-paid contract is not counted again). A contract "
                 "split between agents counts on each agent's row but once in the totals, so the total is no "
                 "more than contracts sold (dbt test fin_commission_contracts_within_sales).",
                 cols=["Agent", "Name", "Territory", "Contracts commissioned (distinct)", "Written",
                       "Expensed (cash basis)", "Chargebacks",
                       "Advances paid", "Advance earned down", "Net", "Advance balance", "Chargeback rate"],
                 rows=rows, wide=True, table_open=True)
    itd = d["commission_itd"]
    itd_rows = [["Agent ledger (Admin Core)" if not r.is_no_agent_ledger else
                 f"Legacy contracts, no agent ledger ({r.source_system})", money_cents(r.written),
                 money_cents(r.chargebacks), money_cents(r.advances_net), money_cents(r.net)]
                for r in itd.itertuples()]
    itd_rows.append(["Cost-split COMMISSION bucket, all contracts", money_cents(itd["written"].sum()),
                     money_cents(itd["chargebacks"].sum()), money_cents(itd["advances_net"].sum()),
                     money_cents(itd["net"].sum())])
    s3 = Section(f"Cost-split commission = agent ledger + legacy unassigned, ITD to {period_label(d['closed'])}",
                 "Every commission dollar in the cost split has a payee or a reconciling line. Ties by source "
                 "system and posting period within rounding (dbt test fin_commission_ties_to_cost_split).",
                 cols=["Line", "Written", "Chargebacks", "Advances not yet earned", "Net"], rows=itd_rows,
                 table_open=True)
    exp, curve = allc["expensed"].sum(), allc["on_curve"].sum()
    s4 = Section("Bridge to the P&L commission expense, trailing 12 months",
                 "The P&L expenses commission as the contract earns it (cost-split earning curve); this statement "
                 "expenses it when it is paid (standard plan) or earned down (advance plan). Commission earned on "
                 "the curve ties to the P&L earned commission by period (dbt test fin_commission_bridges_to_pnl).",
                 cols=["Line", "Amount"],
                 rows=[["Commission expensed, agent ledger (statement)", money_cents(cm["expensed"].sum())],
                       ["Commission expensed, legacy contracts (no agent ledger)", money_cents(nl["expensed"].sum())],
                       ["less: expensed ahead of the earning curve (paid at sale or released on cancel, not yet "
                        "earned)", money_cents(-(exp - curve))],
                       ["Commission earned on the curve = P&L earned commission", money_cents(curve)]],
                 table_open=True)
    kpis = [
        Kpi("Net commission, T12", money(allc["net"].sum()),
            f"{len(cm)} agents {money(cm['net'].sum())}; legacy, no agent ledger {money(nl['net'].sum())}"),
        Kpi("Chargebacks, T12", money(cm["chargebacks"].sum()),
            f"{pct(ratio(-cm['chargebacks'].sum(), cm['written'].sum()))} of commission written (agent ledger)"),
        Kpi("Advance balance", money(cm["advance_balance"].sum()), "advances not yet earned, at period end"),
        Kpi("Commission earned on the curve, T12", money(curve), "the P&L basis; bridge below"),
    ]
    return Page("finance/commissions.html", "Commission statement",
                f"Twelve posting periods {period_label(d['t12_start'])} to {period_label(d['closed'])}.", kpis,
                [s1, s2, s3, s4])


def page_pnl(d: dict) -> Page:
    allp = d["pnl"]
    um = allp[allp["is_unmatched_claims"].astype(bool)]
    p = allp[~allp["is_unmatched_claims"].astype(bool)]
    cat = p.groupby("category", as_index=False)[["earned_premium", "incurred", "acquisition", "admin_fee",
                                                 "program_margin"]].sum()
    s1 = chart_section(
        "Where earned premium went, by product category (T12)",
        "Each bar is earned premium split into claims, acquisition costs (commission, insurer fee, premium "
        "tax), the administration fee and what is left. What is left is the program margin in the table "
        "below: earned premium - claims - acquisition - admin fee.",
        charts.bars(cat["category"], {
            "Claims": cat["incurred"], "Acquisition": cat["acquisition"], "Admin fee": cat["admin_fee"],
            "Left (= program margin)": cat["program_margin"]},
            stacked=True, yfmt="$~s", hover="$,.0f"),
        "fin-pnl-cat", ["Category", "Earned premium", "Claims", "Acquisition", "Admin fee",
                        "Left (= program margin)"],
        [[r.category, money(r.earned_premium), money(r.incurred), money(r.acquisition), money(r.admin_fee),
          money(r.program_margin)] for r in cat.itertuples()], wide=True,
    )
    def line(label, cat_, sub, r) -> list:
        lr = ratio(r["incurred"], r["earned_reserve"])
        comb = ratio(r["incurred"] + r["acquisition"] + r["admin_fee"], r["earned_premium"])
        return [label, cat_, sub, num(r["contracts_written"]), money(r["written"]), money(-r["refunded"]),
                money(r["reinstated"]), money(r["earned_premium"]), money(r["incurred"]),
                {"text": pct(lr), "status": lr_status(lr)}, money(r["acquisition"]), money(r["admin_fee"]),
                money(r["program_margin"]), money(-r["participation"]), money(r["margin_after"]), pct(comb)]

    rows = [line(r["product_code"], r["category"], r["subcategory"], r) for _, r in p.iterrows()]
    sums = ["contracts_written", "written", "refunded", "reinstated", "earned_premium", "earned_reserve",
            "incurred", "acquisition", "admin_fee", "program_margin", "participation", "margin_after", "claim_lines"]
    prod_tot, um_tot, all_tot = p[sums].sum(), um[sums].sum(), allp[sums].sum()
    rows.append(line("Total, products", "", "", prod_tot))
    n_lines = int(um_tot["claim_lines"])
    if n_lines:
        rows.append(line("Claims not matched to a contract", "Reconciling item",
                         f"{n_lines:,} claim lines with no contract key", um_tot))
        rows.append(line("Total, all claims", "", "", all_tot))
    s2 = Section("P&L by product, trailing 12 months",
                 "Refunded is shown negative; Reinstated adds back refunds on cancels that were later reversed, so "
                 "written + refunded + reinstated = net written. Posted loss ratio = incurred / earned claims "
                 "reserve. Program margin = earned premium - claims - acquisition costs - admin fee, so program "
                 "margin / earned premium = 1 - combined ratio. Dealer participation = "
                 "the profit share the dealer entities earned this period (from the profit-share statement). "
                 "The administrator keeps its admin fee plus the margin after participation. Combined ratio = "
                 "(claims + acquisition + admin fee) / earned premium. Acquisition includes commission earned on the "
                 f"contract's earning curve ({money(allp['earned_commission'].sum())}); the Commission statement "
                 "bridges its cash-basis figure to this one.",
                 cols=["Product", "Category", "Subcategory", "Contracts", "Written", "Refunded", "Reinstated",
                       "Earned premium", "Incurred", "Posted loss ratio", "Acquisition", "Admin fee",
                       "Program margin", "Dealer participation", "Margin after participation", "Combined"],
                 rows=rows, wide=True, table_open=True,
                 note=(f"Claims not matched to a contract: {n_lines:,} claim payment lines "
                       f"({money(um_tot['incurred'])} incurred in the period) carry no contract key, so they have "
                       "no product. They are kept as one reconciling line so the total still ties to the claim "
                       "ledger (dbt test fin_pnl_ties_to_core), and they are tracked on the Data quality page "
                       "(claims with no contract).") if n_lines else "")
    lr = ratio(p["incurred"].sum(), p["earned_reserve"].sum())
    lr_ep = ratio(p["incurred"].sum(), p["earned_premium"].sum())
    kpis = [
        Kpi("Net written coverage cost, T12", money(p["net_written"].sum()),
            f"{money(p['written'].sum())} written, {money(-p['refunded'].sum())} refunded, "
            f"{money(p['reinstated'].sum())} reinstated"),
        Kpi("Earned premium, T12", money(p["earned_premium"].sum())),
        Kpi("Incurred claims, T12", money(p["incurred"].sum()),
            f"products; plus {money(um_tot['incurred'])} not matched to a contract" if n_lines else "products"),
        Kpi("Posted loss ratio, T12", pct(lr), "incurred / earned claims reserve", lr_status(lr)),
        Kpi("Loss ratio, T12", pct(lr_ep), "incurred / earned premium"),
        Kpi("Program margin, T12", money(p["program_margin"].sum()),
            f"{pct(ratio(p['program_margin'].sum(), p['earned_premium'].sum()))} of earned premium "
            f"(= 1 - combined ratio); {money(p['margin_after'].sum())} after dealer participation"),
    ]
    return Page("finance/pnl.html", "P&L by product",
                f"Twelve posting periods {period_label(d['t12_start'])} to {period_label(d['closed'])}.", kpis,
                [s1, s2])


def page_remittance(d: dict) -> Page:
    b, dl = d["remit_buckets"], d["remit_dealers"]
    s1 = chart_section(
        "Receipts by days to remit (T12)",
        "How many days passed between a contract being entered and the dealer's cash arriving. Anything past "
        "the standard window is billed a late fee.",
        charts.bars(b["aging_bucket"], {"Receipts": b["receipts"]}, hover=",.0f"),
        "fin-remit-buckets", ["Days to remit", "Receipts", "Amount", "Late fees"],
        [[r.aging_bucket, num(r.receipts), money(r.amount), money(r.late_fee)] for r in b.itertuples()],
    )
    m = _monthly(d["remit_monthly"])
    s2 = chart_section(
        "Late fees billed by month",
        "Late fees are a share of the late remittance, billed in the posting period the cash arrives.",
        charts.lines(m, "month", {"late_fee": "Late fees"}, yfmt="$~s", hover="$,.2f"),
        "fin-remit-monthly", ["Period", "Late fees", "Late receipts", "Receipts"],
        [[period_label(r.posting_period_key), money(r.late_fee), num(r.late_receipts), num(r.receipts)]
         for r in m.itertuples()][::-1],
    )
    rows = [[r.dealer_number, r.dealer_name, num(r.receipts), num(r.late_receipts),
             pct(ratio(r.late_receipts, r.receipts)), num(r.avg_age, 1), money(r.amount), money(r.late_fee)]
            for r in dl.itertuples()]
    s3 = Section("Late-fee bill by dealer, trailing 12 months", "Sorted by late fees billed.",
                 cols=["Dealer", "Name", "Receipts", "Late", "Late share", "Avg days", "Remitted", "Late fees"],
                 rows=rows, wide=True, table_open=True)
    kpis = [
        Kpi("Cash remitted, T12", money(b["amount"].sum()), f"{num(b['receipts'].sum())} receipts"),
        Kpi("Late fees billed, T12", money(b["late_fee"].sum())),
        Kpi("Late receipts", pct(ratio(dl["late_receipts"].sum(), dl["receipts"].sum())), "of all receipts"),
    ]
    return Page("finance/remittance.html", "Remittance aging and late fees",
                f"Twelve posting periods {period_label(d['t12_start'])} to {period_label(d['closed'])}.", kpis,
                [s1, s2, s3, *v6.settlement_sections(d, period_label(d["closed"]))])


def page_controls(d: dict) -> Page:
    c = d["controls"]
    recent = c[c["posting_period_key"].between(d["t12_start"], d["closed"])]
    span = f"{period_label(d['t12_start'])} to {period_label(d['closed'])}"
    rows = [[period_label(r.posting_period_key), r.source_system, r.txn_type, num(r.txn_count),
             money_cents(r.txn_amount), money_cents(r.split_amount), money_cents(r.difference),
             num(r.rounding_txns),
             {"text": "Balanced" if r.is_balanced else "Break", "status": "good" if r.is_balanced else "critical"}]
            for r in recent.itertuples()]
    s1 = Section(f"Cost-split control totals, last 12 closed periods ({span})",
                 "Each posted transaction must equal the sum of its cost split (admin fee, insurer fee, premium "
                 "tax, commission, claims reserve, profit-share reserve). Penny differences from per-bucket "
                 "rounding are counted, not hidden.",
                 cols=["Period", "Source", "Type", "Transactions", "Txn amount", "Split total", "Difference",
                       "Rounded txns", "Status"], rows=rows, wide=True, table_open=True)
    rf = d["rollforward"]
    s2 = Section("Claims-reserve roll-forward, whole book",
                 "Opening + net written - claims paid - surplus released = closing funded reserve. Surplus "
                 "released is the part of each annual profit-share distribution that the profit-share reserve "
                 "does not cover, so it comes out of the claims reserve. Unearned = net written not yet earned.",
                 cols=["Period", "Opening", "Net written", "Claims paid", "Surplus released", "Closing", "Earned",
                       "Unearned"],
                 rows=[[period_label(r.posting_period_key), money(r.opening), money(r.written), money(r.paid),
                        money(r.released), money(r.closing), money(r.earned), money(r.unearned)]
                       for r in rf.itertuples()],
                 wide=True, table_open=True)
    ce, rr = d["cession"], d["reserve_recon"]
    g = lambda col: float(rr[col].sum())  # noqa: E731
    part, rest, book = g("participation_reserve_balance"), g("rest_of_book_reserve_balance"), \
        g("book_closing_funded_reserve")
    unearned, surplus = g("book_closing_unearned_reserve"), g("earned_surplus")
    diff, sdiff = g("difference"), g("earned_surplus_difference")
    status = lambda v: {"text": money_cents(v), "status": "good" if abs(v) < 0.005 else "critical"}  # noqa: E731
    s3 = Section(f"Funded reserve reconciliation, {period_label(d['closed'])}",
                 "Ties the Cession page's participation reserve balance plus the rest of the book (recomputed from "
                 "the cost split and claim payments on contracts with no participation entity) to the book-wide "
                 "roll-forward, then checks the closing funded reserve against unearned reserve plus the earned "
                 "surplus recomputed from earned reserve, claims paid and distributions. Every line is computed on "
                 "its own; the differences must be $0.00 (dbt test fin_reserve_rollforward_ties_to_cession).",
                 cols=["Line", "Amount"],
                 rows=[[f"Participation reserve balance ({len(ce)} entities, Cession page)", money_cents(part)],
                       ["Contracts with no participation entity: reserve written less claims paid",
                        money_cents(rest)],
                       ["of which claims paid with no contract key", money_cents(-g("unmatched_claims_paid"))],
                       ["Participation + rest of book", money_cents(part + rest)],
                       ["Closing funded reserve, whole book (roll-forward)", money_cents(book)],
                       ["Difference", status(diff)],
                       ["Unearned reserve (roll-forward)", money_cents(unearned)],
                       ["Earned surplus: earned reserve - claims paid - surplus released, ITD", money_cents(surplus)],
                       ["Difference: closing funded - unearned - earned surplus", status(sdiff)]])
    bad = int((~c["is_balanced"]).sum())
    closed_c = c[c["posting_period_key"] <= d["closed"]]
    first = period_label(closed_c["posting_period_key"].min()) if len(closed_c) else "n/a"
    kpis = [
        Kpi("Control lines", num(len(closed_c)),
            f"period x source x transaction type, {first} to {period_label(d['closed'])} (closed periods)"),
        Kpi("Breaks", str(bad), "outside rounding tolerance, all periods incl. the open one",
            "good" if bad == 0 else "critical"),
        Kpi("Rounding residual", money_cents(c["rounding_residual"].fillna(0).sum()),
            f"{num(c['rounding_txns'].sum())} transactions, all periods"),
    ]
    return Page("finance/controls.html", "Close controls",
                "Control totals the finance team checks before any statement leaves the team.", kpis,
                [s1, *v7.group_section(d, money_cents, span), s2, s3, *controls_v6(d)])


def controls_v6(d: dict) -> list[Section]:
    """V6 bridge and rate-card sections; the bridge also shows cancels the source reversed (V7 edit 23)."""
    out = v6.control_sections(d, money_cents)
    if out:
        extra = v7.reversal_rows(d, money_cents)
        out[0].rows[4:4] = extra  # after step 4, which already holds them, before the Experience total
    return out


# --------------------------------------------------------------------------- excel
def write_workbook(d: dict, path: Path) -> Path:
    """Inputs first (the seed rules), then one output tab per statement. No formulas cross sheets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    readme = pd.DataFrame({"item": [
        "Layout", "Inputs", "Calculations", "Outputs", "Period", "Source"], "detail": [
        "Inputs tabs, then Outputs tabs; every value is a number, nothing links across sheets.",
        "Rules from the finance seeds: structure terms, P&L line map, aging buckets, control tolerances.",
        "Done in the warehouse (dbt models under models/marts/finance) and tested against the core facts.",
        "Cession, profit share, commission (with its tie to the cost split), P&L, remittance, controls, reserve "
        "roll-forward and the funded reserve reconciliation; then Experience, New Reserves, the reserve bridge, "
        "commission hierarchy, rate card (term first, then odometer band) with its exceptions, the partner "
        "settlement statement and the remit register.",
        f"Statements at {period_label(d['closed'])}; trailing 12 = {period_label(d['t12_start'])} to "
        f"{period_label(d['closed'])}.",
        "Synthetic data generated by the pipeline."]})
    sheets = {"README": readme}
    for seed in SEEDS:
        sheets[f"Input_{seed}"[:31]] = d[f"seed_{seed}"]
    sheets.update({
        "Cession_ITD": d["cession"], "ProfitShare_Statement": d["statement"], "Commission_T12": d["commission"],
        "PnL_T12": d["pnl"], "Remittance_T12": d["remit_dealers"], "Controls": d["controls"],
        "Reserve_Rollforward": d["rollforward"], "Commission_vs_CostSplit": d["commission_itd"],
        "Reserve_Reconciliation": d["reserve_recon"],
        **v6.sheets(d),
    })
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        for name, df in sheets.items():
            out = excel_frame(df)
            out.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for i, col in enumerate(out.columns, start=1):
                fmt = excel_format(col, out[col])
                if fmt:
                    for (cell,) in ws.iter_rows(min_row=2, min_col=i, max_col=i):
                        cell.number_format = fmt
    return path


HUMAN_KEYS = ["entity_id", "entity_name", "dealer_number", "dealer_name", "agent_code", "agent_name",
              "product_code", "posting_period_key"]
MONEY_FMT = "#,##0.00;-#,##0.00"
PCT_FMT = "0.0%"
COUNT_FMT = "#,##0"
COUNT_WORDS = ("contracts", "receipts", "lines", "count", "txns", "months", "periods", "sort", "days")


def excel_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Statement-ready tab: no surrogate hash keys, human keys first, money rounded to cents."""
    out = df.copy()
    hashed = [c for c in out.columns if c.endswith("_key") and c not in ("posting_period_key", "tenant_key")]
    out = out.drop(columns=hashed)
    first = [c for c in HUMAN_KEYS if c in out.columns]
    out = out[first + [c for c in out.columns if c not in first]]
    for col in out.columns:
        s = out[col]
        if isinstance(s.dtype, pd.DatetimeTZDtype):
            out[col] = s.dt.tz_localize(None)
        elif pd.api.types.is_float_dtype(s):
            out[col] = s.round(6 if _is_ratio(col) else 2) + 0.0  # + 0.0 turns -0.0 into 0.0
    return out


def _is_ratio(col: str) -> bool:
    return col.endswith(("_pct", "_ratio", "_rate", "pct_cumulative")) or col in ("ratio", "loss_ratio", "weight")


def excel_format(col: str, s: pd.Series) -> str | None:
    if not pd.api.types.is_numeric_dtype(s) or pd.api.types.is_bool_dtype(s):
        return None
    if col.endswith(("_key", "_id", "_number", "_month")):
        return None
    if _is_ratio(col):
        return PCT_FMT
    if any(w in col for w in COUNT_WORDS) or not pd.api.types.is_float_dtype(s):
        return COUNT_FMT
    return MONEY_FMT


# --------------------------------------------------------------------------- plug-in entry point
def build_pages(d: dict) -> list[Page]:
    span = f"{period_label(d['t12_start'])} to {period_label(d['closed'])}"
    hierarchy = v6.page_commission_hierarchy(d, span)
    stamp, unassigned = v7.commission_stamp(d)  # V7 edit 28: which month, as of when, and the Unassigned row
    hierarchy.lede += stamp
    hierarchy.sections[1:1] = unassigned
    return [page_index(d), page_cession(d), page_profit_share(d), page_commissions(d), page_pnl(d),
            page_remittance(d), page_controls(d), v6.page_experience(d, period_label(d["closed"]), lr_status),
            v6.page_new_reserves(d, span), hierarchy, *v7.pages(d, span)]


def build(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list[Page]:
    """Report-shell plug-in: write the Excel pack under ``out_dir/downloads/finance`` and return pages."""
    d = load(con)
    write_workbook(d, Path(out_dir) / "downloads" / AREA / WORKBOOK)
    v7.write_pack(d, out_dir)
    return build_pages(d)
