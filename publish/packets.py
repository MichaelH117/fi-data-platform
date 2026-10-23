"""Per-dealer and per-program publish packets (docs/V3_DESIGN.md section 3, per-dealer publish rule).

Dealer packets: one per (tenant, reportable dealer) in ``core.dim_user_dealer_access`` - the same
user -> dealer, tenant map the semantic RLS table uses - written to
``<packets_dir>/<tenant_code>/<dealer_number>/``. Program packets: one per tenant (white-label partner
program), written to ``<packets_dir>/<tenant_code>/`` (``index.html`` + ``program.xlsx``), listing that
program's dealers with links to their packets.

Every table reads a certified mart, never a fact, so a packet KPI is the same number the reports show
("one certified source per KPI"):

    kpi                 headline strip: T12 written, cancel rate, loss ratio and its peer percentile,
                        in force, pending, late fees        dealer.mart_dealer_scorecard (+ production,
                                                            pending marts) + peer_benchmark_t12
    production          month by month, T12                 dealer.mart_dealer_scorecard
    pending_contracts   PENDING contracts, no customer data dealer.mart_dealer_pending_contracts
    remittance_aging    shared seed bands (remit_aging_buckets), latest closed period and T12
                                                            dealer.mart_remittance_aging
    claims_kpi          T12 claims, early claims, loss ratio dealer.mart_dealer_scorecard
    program_claims      program packet only, by product line claims.mart_claims_kpi (tenant grain;
                                                            it has no dealer grain, so dealer packets
                                                            take claims from the scorecard)

Every query is filtered by BOTH dealer_key and tenant_key (program queries by tenant_key), so a
packet can only ever hold its own dealer's rows. CSVs keep machine column names; HTML and xlsx use
readable headers, money / count / percent formats and a period label on every table. No hand exports.
"""

from __future__ import annotations

import html
import json
import logging
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

log = logging.getLogger("publish.packets")

_DEALER = "dealer_key = $dealer_key and tenant_key = $tenant_key"
_T12 = "month_start between $t12_start and $t12_end"


@dataclass(frozen=True)
class Col:
    key: str
    label: str
    fmt: str = "text"  # text | int | money | pct | ratio | date


@dataclass(frozen=True)
class Spec:
    title: str
    period: str  # t12 | asof | aging | program_t12
    sql: str
    cols: tuple[Col, ...]


KPI_COLS: tuple[Col, ...] = (
    Col("contracts_written", "Contracts written", "int"),
    Col("cancel_rate", "Cancel rate, by sale cohort, T12", "pct"),  # = reports.dealer.watch.CANCEL_RATE_T12
    Col("loss_ratio", "Loss ratio", "pct"),
    Col("peer_loss_ratio", "Peer loss ratio (VSC)", "pct"),
    Col("loss_ratio_peer_pct", "Loss-ratio peer percentile", "pct"),
    Col("contracts_in_force", "In force", "int"),
    Col("pending_contracts", "Pending contracts", "int"),
    Col("late_fees", "Late fees", "money"),
)

TABLE_SPECS: dict[str, Spec] = {
    "kpi": Spec(
        "Headline KPIs", "t12",
        f"""
        with s as (select * from dealer.mart_dealer_scorecard where {_DEALER} and {_T12})
        select cast(sum(contracts_written) as bigint) as contracts_written,
               cast(sum(contracts_cancelled) as bigint) as contracts_cancelled,
               sum(contracts_cancelled) / nullif(sum(contracts_written), 0) as cancel_rate,
               round(sum(earned_claims_reserve), 2) as earned_claims_reserve,
               round(sum(incurred_claims), 2) as incurred_claims,
               sum(incurred_claims) / nullif(sum(earned_claims_reserve), 0) as loss_ratio,
               (select max(t12_loss_ratio_peer_pct) from dealer.mart_dealer_scorecard
                 where {_DEALER} and month_start = $t12_end) as loss_ratio_peer_pct,
               (select cast(coalesce(sum(contracts_in_force), 0) as bigint) from dealer.mart_dealer_production
                 where {_DEALER}) as contracts_in_force,
               (select count(*) from dealer.mart_dealer_pending_contracts where {_DEALER}) as pending_contracts,
               cast(sum(remittances) as bigint) as remittances,
               round(sum(late_fees), 2) as late_fees,
               cast(sum(claims_reported) as bigint) as claims_reported
        from s
        having count(*) > 0
        """,
        KPI_COLS,
    ),
    "production": Spec(
        "Production", "t12",
        f"""
        select strftime(month_start, '%b %Y') as month,
               cast(contracts_written as bigint) as contracts_written,
               cast(contracts_cancelled as bigint) as contracts_cancelled,
               cancel_rate, round(coverage_cost, 2) as coverage_cost, round(retail_price, 2) as retail_price,
               round(earned_claims_reserve, 2) as earned_claims_reserve,
               round(incurred_claims, 2) as incurred_claims, loss_ratio
        from dealer.mart_dealer_scorecard
        where {_DEALER} and {_T12}
        order by month_start
        """,
        (Col("month", "Month"), Col("contracts_written", "Contracts written", "int"),
         Col("contracts_cancelled", "Contracts cancelled", "int"),
         Col("cancel_rate", "Cancel rate, by sale cohort, month", "pct"),
         Col("coverage_cost", "Coverage cost", "money"), Col("retail_price", "Retail price", "money"),
         Col("earned_claims_reserve", "Earned claims reserve", "money"),
         Col("incurred_claims", "Incurred claims", "money"),
         Col("loss_ratio", "Loss ratio", "pct")),
    ),
    "pending_contracts": Spec(
        "Pending contracts", "asof",
        f"""
        select contract_number, product, plan, date_sold, cast(days_pending as bigint) as days_pending,
               pending_age_band, round(coverage_cost, 2) as coverage_cost, round(retail_price, 2) as retail_price
        from dealer.mart_dealer_pending_contracts
        where {_DEALER}
        order by days_pending desc, contract_number
        """,
        (Col("contract_number", "Contract"), Col("product", "Product"), Col("plan", "Plan"),
         Col("date_sold", "Date sold", "date"), Col("days_pending", "Days pending", "int"),
         Col("pending_age_band", "Age band"), Col("coverage_cost", "Coverage cost", "money"),
         Col("retail_price", "Retail price", "money")),
    ),
    "remittance_aging": Spec(
        "Remittance aging", "aging",
        f"""
        select age_band_order, age_band,
               cast(coalesce(sum(cash_transactions) filter (where posting_period_key = $p_end), 0) as bigint)
                   as period_remittances,
               round(coalesce(sum(remitted_amount) filter (where posting_period_key = $p_end), 0), 2)
                   as period_amount,
               round(coalesce(sum(late_fees) filter (where posting_period_key = $p_end), 0), 2) as period_late_fees,
               cast(sum(cash_transactions) as bigint) as t12_remittances,
               cast(sum(late_transactions) as bigint) as t12_late_remittances,
               round(sum(remitted_amount), 2) as t12_amount,
               round(sum(late_fees), 2) as t12_late_fees
        from dealer.mart_remittance_aging
        where {_DEALER} and posting_period_key between $p_start and $p_end
        group by all
        order by age_band_order
        """,
        (Col("age_band", "Days to remit"), Col("period_remittances", "{p} remittances", "int"),
         Col("period_amount", "{p} amount", "money"), Col("period_late_fees", "{p} late fees", "money"),
         Col("t12_remittances", "T12 remittances", "int"), Col("t12_late_remittances", "T12 late", "int"),
         Col("t12_amount", "T12 amount", "money"), Col("t12_late_fees", "T12 late fees", "money")),
    ),
    "claims_kpi": Spec(
        "Claims KPIs", "t12",
        f"""
        select cast(sum(claims_reported) as bigint) as claims_reported,
               cast(sum(early_claims) as bigint) as early_claims,
               sum(early_claims) / nullif(sum(claims_reported), 0) as early_claim_rate,
               round(sum(incurred_claims), 2) as incurred_claims,
               round(sum(earned_claims_reserve), 2) as earned_claims_reserve,
               sum(incurred_claims) / nullif(sum(earned_claims_reserve), 0) as loss_ratio
        from dealer.mart_dealer_scorecard
        where {_DEALER} and {_T12}
        having count(*) > 0
        """,
        (Col("claims_reported", "Claims reported", "int"), Col("early_claims", "Early claims", "int"),
         Col("early_claim_rate", "Early-claim rate", "pct"), Col("incurred_claims", "Incurred claims", "money"),
         Col("earned_claims_reserve", "Earned claims reserve", "money"), Col("loss_ratio", "Loss ratio", "pct")),
    ),
}

PROGRAM_CLAIMS = Spec(
    "Program claims KPIs by product line", "program_t12",
    f"""
    select coalesce(subcategory_code, 'All products') as product_line,
           cast(sum(claims_reported) as bigint) as claims_reported,
           cast(sum(claims_denied) as bigint) as claims_denied,
           sum(claims_denied) / nullif(sum(claims_reported), 0) as denial_rate,
           cast(sum(paid_claim_count) as bigint) as paid_claims,
           round(sum(paid_amount), 2) as paid_amount,
           sum(paid_amount_on_paid_claims) / nullif(sum(paid_claim_count), 0) as claim_severity,
           sum(claim_count) / nullif(sum(exposure_years), 0) as claim_frequency
    from claims.mart_claims_kpi
    where tenant_key = $tenant_key and {_T12}
    group by grouping sets ((subcategory_code), ())
    order by grouping(subcategory_code), 1
    """,
    (Col("product_line", "Product line"), Col("claims_reported", "Claims reported", "int"),
     Col("claims_denied", "Denied", "int"), Col("denial_rate", "Denial rate", "pct"),
     Col("paid_claims", "Paid claims", "int"), Col("paid_amount", "Paid", "money"),
     Col("claim_severity", "Severity", "money"), Col("claim_frequency", "Frequency (per exposure year)", "ratio")),
)

# Back-compat name: the table list the packet is made of.
TABLES: dict[str, str] = {name: s.sql for name, s in TABLE_SPECS.items()}

PROGRAM_KPI_COLS: tuple[Col, ...] = (
    Col("dealers", "Dealers", "int"),
    Col("contracts_written", "Contracts written", "int"),
    Col("contracts_in_force", "In force", "int"),
    Col("earned_claims_reserve", "Earned claims reserve", "money"),
    Col("incurred_claims", "Incurred claims", "money"),
    Col("loss_ratio", "Loss ratio", "pct"),
    Col("late_fees", "Late fees", "money"),
    Col("pending_contracts", "Pending contracts", "int"),
)
PROGRAM_SUM_KEYS = ("contracts_written", "contracts_cancelled", "contracts_in_force", "earned_claims_reserve",
                    "incurred_claims", "late_fees", "pending_contracts", "remittances", "claims_reported")
PROGRAM_DEALER_COLS: tuple[Col, ...] = (
    Col("dealer_number", "Dealer"), Col("dealer_name", "Name"), Col("contracts_written", "Written", "int"),
    Col("cancel_rate", "Cancel rate, by sale cohort, T12", "pct"), Col("loss_ratio", "Loss ratio", "pct"),
    Col("loss_ratio_peer_pct", "Peer percentile", "pct"), Col("contracts_in_force", "In force", "int"),
    Col("pending_contracts", "Pending", "int"), Col("late_fees", "Late fees", "money"),
)


@dataclass(frozen=True)
class Packet:
    tenant_key: int
    tenant_code: str
    reportable_name: str
    primary_color: str
    secondary_color: str
    dealer_key: str
    dealer_number: str
    dealer_name: str


@dataclass(frozen=True)
class Periods:
    as_of: date
    t12_start: date  # first month of the trailing 12 complete months
    t12_end: date  # last complete month

    @property
    def p_start(self) -> int:
        return self.t12_start.year * 100 + self.t12_start.month

    @property
    def p_end(self) -> int:
        return self.t12_end.year * 100 + self.t12_end.month

    @property
    def month_label(self) -> str:
        return f"{self.t12_end:%b %Y}"

    @property
    def t12_label(self) -> str:
        return f"T12 to {self.t12_end:%b %Y}"

    def label(self, kind: str) -> str:
        return {
            "t12": f"{self.t12_label} ({self.t12_start:%b %Y} - {self.t12_end:%b %Y})",
            "program_t12": f"{self.t12_label}, all dealers in the program",
            "asof": f"As of {self.as_of:%Y-%m-%d}",
            "aging": f"{self.month_label} posting period and {self.t12_label}; bands from the shared aging seed",
        }[kind]

    def params(self) -> dict[str, object]:
        return {"t12_start": self.t12_start, "t12_end": self.t12_end, "p_start": self.p_start, "p_end": self.p_end}


def periods(con: duckdb.DuckDBPyConnection) -> Periods:
    as_of, last = con.execute("select as_of_date, last_complete_month from core.dim_reporting_date").fetchone()
    last = pd.Timestamp(last).date()
    start = (pd.Timestamp(last) - pd.DateOffset(months=11)).date()
    return Periods(pd.Timestamp(as_of).date(), start, last)


def packets(con: duckdb.DuckDBPyConnection) -> list[Packet]:
    rows = con.execute(
        """
        select distinct a.tenant_key, t.tenant_code, t.reportable_name, coalesce(t.primary_color, '#1f3a5f'),
               coalesce(t.secondary_color, '#e8eef5'), a.dealer_key, a.dealer_number, d.dealer_name
        from core.dim_user_dealer_access a
        join core.dim_tenant t using (tenant_key)
        join core.dim_dealer d using (dealer_key)
        order by 2, 7
        """
    ).fetchall()
    return [Packet(*r) for r in rows]


def _dealer_state(con: duckdb.DuckDBPyConnection, dealer_key: str) -> str | None:
    row = con.execute("select state from core.dim_dealer where dealer_key = ?", [dealer_key]).fetchone()
    return row[0] if row else None


def peer_loss_ratio(peer: pd.DataFrame | None, state: str | None) -> tuple[float | None, str]:
    """T12 peer loss ratio from the suppressed benchmark: the dealer's state cell, else all states."""
    if peer is None or peer.empty:
        return None, ""
    cells = peer[(peer["coverage_tier"] == "ALL") & (peer["term_band"] == "ALL") & ~peer["suppressed"].astype(bool)]
    for scope, label in ((state, f"{state} peers"), ("ALL", "all-state peers")):
        hit = cells[cells["state"] == scope]
        if scope and len(hit) and pd.notna(hit["loss_ratio"].iloc[0]):
            return float(hit["loss_ratio"].iloc[0]), label
    return None, ""


def packet_tables(con: duckdb.DuckDBPyConnection, p: Packet, per: Periods | None = None,
                  peer: pd.DataFrame | None = None) -> dict[str, pd.DataFrame]:
    per = per or periods(con)
    params = {"dealer_key": p.dealer_key, "tenant_key": p.tenant_key, **per.params()}
    out: dict[str, pd.DataFrame] = {}
    for name, spec in TABLE_SPECS.items():
        sql_params = {k: v for k, v in params.items() if f"${k}" in spec.sql}
        out[name] = con.execute(spec.sql, sql_params).df()
    kpi = out["kpi"]
    if len(kpi):
        value, scope = peer_loss_ratio(peer, _dealer_state(con, p.dealer_key))
        kpi["peer_loss_ratio"] = value
        kpi["peer_scope"] = scope
    return out


# --------------------------------------------------------------------------- formatting
def _missing(v: object) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or v is pd.NA or v is pd.NaT


def fmt_value(v: object, fmt: str) -> str:
    if _missing(v):
        return "-"
    if fmt == "int":
        return f"{int(v):,}"
    if fmt == "money":
        return f"${float(v):,.2f}"
    if fmt == "pct":
        return f"{float(v):.1%}"
    if fmt == "ratio":
        return f"{float(v):.4f}"
    if fmt == "date":
        return f"{pd.Timestamp(v):%Y-%m-%d}"
    return str(v)


XLSX_FORMATS = {"int": "#,##0", "money": '"$"#,##0.00', "pct": "0.0%", "ratio": "0.0000", "date": "yyyy-mm-dd"}


def _labels(cols: tuple[Col, ...], per: Periods) -> list[str]:
    return [c.label.format(p=per.month_label) for c in cols]


def _table_html(df: pd.DataFrame, cols: tuple[Col, ...], per: Periods, links: dict[str, str] | None = None) -> str:
    if not len(df):
        return "<p class='empty'>No rows for this period.</p>"
    head = "".join(f"<th class='{'num' if c.fmt != 'text' and c.fmt != 'date' else ''}'>{html.escape(lab)}</th>"
                   for c, lab in zip(cols, _labels(cols, per), strict=True))
    body = []
    for rec in df.to_dict("records"):
        cells = []
        for c in cols:
            text = html.escape(fmt_value(rec.get(c.key), c.fmt))
            if links and c.key == "dealer_number" and rec.get(c.key) in links:
                text = f"<a href='{html.escape(links[rec[c.key]])}'>{text}</a>"
            cells.append(f"<td class='{'num' if c.fmt not in ('text', 'date') else ''}'>{text}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f"<div class='scroll'><table class='t'><thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>")


def _kpi_strip(row: dict, cols: tuple[Col, ...], note: str = "") -> str:
    tiles = "".join(f"<div class='kpi'><span>{html.escape(c.label)}</span>"
                    f"<b>{html.escape(fmt_value(row.get(c.key), c.fmt))}</b></div>" for c in cols)
    return f"<div class='kpis'>{tiles}</div>" + (f"<p class='note'>{html.escape(note)}</p>" if note else "")


def _page(title: str, brand: str, tint: str, header: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root {{ --brand: {brand}; --tint: {tint}; --ink: #1d2733; --muted: #5b6673; --line: #d8dee6;
         --bg: #fff; --card: #f6f8fa; }}
body {{ font-family: system-ui, sans-serif; margin: 0 16px 32px; color: var(--ink); background: var(--bg); }}
header {{ background: var(--brand); color: #fff; padding: 16px; margin: 0 -16px 16px; }}
header a {{ color: #fff; }}
h2 {{ margin: 24px 0 2px; font-size: 18px; }}
.period {{ margin: 0 0 8px; color: var(--muted); font-size: 13px; }}
.kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }}
.kpi {{ background: var(--card); border-left: 4px solid var(--brand); padding: 8px 10px; }}
.kpi span {{ display: block; font-size: 12px; color: var(--muted); }}
.kpi b {{ font-size: 20px; font-variant-numeric: tabular-nums; }}
.note, .empty {{ color: var(--muted); font-size: 13px; }}
.scroll {{ overflow-x: auto; }}
table.t {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
table.t th {{ background: var(--tint); text-align: left; }}
table.t th, table.t td {{ padding: 4px 8px; border-bottom: 1px solid var(--line); white-space: nowrap; }}
table.t .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
</style></head><body>
<header>{header}</header>
{body}
</body></html>
"""


def _section(title: str, period: str, inner: str) -> str:
    return f"<h2>{html.escape(title)}</h2>\n<p class='period'>{html.escape(period)}</p>\n{inner}"


def _peer_note(kpi: pd.DataFrame) -> str:
    if not len(kpi):
        return ""
    scope = kpi["peer_scope"].iloc[0] if "peer_scope" in kpi else ""
    return ("Loss ratio = incurred claims / earned claims reserve. Peer loss ratio: VSC contracts, "
            f"{scope or 'not published (suppressed)'}, from the suppressed T12 peer benchmark. "
            "Peer percentile ranks this dealer's T12 loss ratio among all dealers with enough earned reserve "
            "(mart_dealer_scorecard; 0% = lowest).")


def _html(p: Packet, tables: dict[str, pd.DataFrame], per: Periods) -> str:
    kpi = tables["kpi"]
    parts = [_section("Headline KPIs", per.label("t12"),
                      _kpi_strip(kpi.iloc[0].to_dict(), KPI_COLS, _peer_note(kpi)) if len(kpi)
                      else "<p class='empty'>No activity in this period.</p>")]
    for name, spec in TABLE_SPECS.items():
        if name != "kpi":
            parts.append(_section(spec.title, per.label(spec.period), _table_html(tables[name], spec.cols, per)))
    header = (f"<strong>{html.escape(p.reportable_name)}</strong><br>Dealer packet: {html.escape(p.dealer_number)} "
              f"{html.escape(p.dealer_name or '')} &middot; {html.escape(per.t12_label)} &middot; "
              f"data as of {per.as_of:%Y-%m-%d} &middot; <a href='../index.html'>program</a>")
    return _page(f"{p.dealer_number} packet", p.primary_color, p.secondary_color, header, "\n".join(parts))


def _xlsx(path: Path, sheets: list[tuple[str, str, str, pd.DataFrame, tuple[Col, ...]]], per: Periods) -> bool:
    """sheets: (sheet name, title, period label, frame, columns). Readable headers and number formats."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
    except ImportError:
        return False
    wb = Workbook()
    wb.remove(wb.active)
    for sheet, title, period, df, cols in sheets:
        ws = wb.create_sheet(sheet[:31])
        ws.append([title])
        ws["A1"].font = Font(bold=True, size=13)
        ws.append([period])
        ws.append([])
        ws.append(_labels(cols, per))
        for cell in ws[4]:
            cell.font = Font(bold=True)
        for rec in df.to_dict("records"):
            row = []
            for c in cols:
                v = rec.get(c.key)
                if _missing(v):
                    v = None
                elif c.fmt == "int":
                    v = int(v)
                elif c.fmt in ("money", "pct", "ratio"):
                    v = float(v)
                elif c.fmt == "date":
                    v = pd.Timestamp(v).date()
                row.append(v)
            ws.append(row)
        for j, c in enumerate(cols, start=1):
            letter = ws.cell(row=4, column=j).column_letter
            ws.column_dimensions[letter].width = max(12, len(_labels(cols, per)[j - 1]) + 2)
            if c.fmt in XLSX_FORMATS:
                for r in range(5, ws.max_row + 1):
                    ws.cell(row=r, column=j).number_format = XLSX_FORMATS[c.fmt]
        if not len(df):
            ws.append(["No rows for this period."])
    wb.save(path)
    return True


def write_packet(out_dir: Path, p: Packet, tables: dict[str, pd.DataFrame], per: Periods) -> Path:
    d = out_dir / p.tenant_code / p.dealer_number
    d.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(d / f"{name}.csv", index=False)
    (d / "index.html").write_text(_html(p, tables, per), encoding="utf-8")
    sheets = [(name, spec.title, per.label(spec.period), tables[name], spec.cols) for name, spec in TABLE_SPECS.items()]
    has_xlsx = _xlsx(d / "packet.xlsx", sheets, per)
    meta = {"tenant_code": p.tenant_code, "dealer_number": p.dealer_number, "dealer_key": p.dealer_key,
            "as_of": str(per.as_of), "period": per.t12_label, "tables": {k: len(v) for k, v in tables.items()},
            "xlsx": has_xlsx}
    (d / "packet.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return d


# --------------------------------------------------------------------------- program packets
def program_tables(con: duckdb.DuckDBPyConnection, members: list[tuple[Packet, dict[str, pd.DataFrame]]],
                   per: Periods) -> dict[str, pd.DataFrame]:
    """Program (tenant) tables built from its dealer packets' KPI rows, so totals tie by construction,
    plus claims.mart_claims_kpi at tenant grain."""
    rows = []
    for p, t in members:
        k = t["kpi"].iloc[0].to_dict() if len(t["kpi"]) else {}
        rows.append({"dealer_number": p.dealer_number, "dealer_name": p.dealer_name, **k})
    dealers = pd.DataFrame(rows, columns=["dealer_number", "dealer_name", *PROGRAM_SUM_KEYS, "cancel_rate",
                                          "loss_ratio", "loss_ratio_peer_pct"]) if rows else pd.DataFrame()
    total: dict[str, object] = {"dealers": len(members)}
    for key in PROGRAM_SUM_KEYS:
        total[key] = float(pd.to_numeric(dealers[key], errors="coerce").fillna(0).sum()) if len(dealers) else 0.0
    ecr = total["earned_claims_reserve"]
    total["loss_ratio"] = total["incurred_claims"] / ecr if ecr else None
    tenant_key = members[0][0].tenant_key if members else -1
    claims = con.execute(PROGRAM_CLAIMS.sql, {"tenant_key": tenant_key, "t12_start": per.t12_start,
                                              "t12_end": per.t12_end}).df()
    return {"program_kpi": pd.DataFrame([total]), "dealers": dealers, "program_claims": claims}


def write_program(out_dir: Path, tenant: Packet, tables: dict[str, pd.DataFrame], per: Periods) -> Path:
    d = out_dir / tenant.tenant_code
    d.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.to_csv(d / f"program_{name}.csv", index=False)
    links = {n: f"{n}/index.html" for n in tables["dealers"].get("dealer_number", [])}
    body = "\n".join([
        _section("Program totals", per.label("program_t12"),
                 _kpi_strip(tables["program_kpi"].iloc[0].to_dict(), PROGRAM_KPI_COLS,
                            "Written, earned, incurred and late fees are T12 sums of the dealer packets below; "
                            f"in force and pending are as of {per.as_of:%Y-%m-%d}. "
                            "Loss ratio = incurred / earned claims reserve.")),
        _section("Dealers", per.label("t12"), _table_html(tables["dealers"], PROGRAM_DEALER_COLS, per, links)),
        _section(PROGRAM_CLAIMS.title, per.label("program_t12"),
                 _table_html(tables["program_claims"], PROGRAM_CLAIMS.cols, per)),
    ])
    header = (f"<strong>{html.escape(tenant.reportable_name)}</strong><br>Partner program packet: "
              f"{html.escape(tenant.tenant_code)} &middot; {len(tables['dealers'])} dealers &middot; "
              f"{html.escape(per.t12_label)} &middot; data as of {per.as_of:%Y-%m-%d}")
    (d / "index.html").write_text(_page(f"{tenant.tenant_code} program packet", tenant.primary_color,
                                        tenant.secondary_color, header, body), encoding="utf-8")
    has_xlsx = _xlsx(d / "program.xlsx", [
        ("program", "Program totals", per.label("program_t12"), tables["program_kpi"], PROGRAM_KPI_COLS),
        ("dealers", "Dealers", per.label("t12"), tables["dealers"], PROGRAM_DEALER_COLS),
        ("claims", PROGRAM_CLAIMS.title, per.label("program_t12"), tables["program_claims"], PROGRAM_CLAIMS.cols),
    ], per)
    meta = {"tenant_code": tenant.tenant_code, "as_of": str(per.as_of), "period": per.t12_label,
            "dealers": tables["dealers"].get("dealer_number", pd.Series(dtype=str)).tolist(), "xlsx": has_xlsx}
    (d / "program.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return d


def build_packets(con: duckdb.DuckDBPyConnection, out_dir: Path, peer: pd.DataFrame | None = None) -> list[Path]:
    """Write every dealer packet, then one program packet per tenant. Returns the dealer packet dirs."""
    per = periods(con)
    written: list[Path] = []
    by_tenant: dict[str, list[tuple[Packet, dict[str, pd.DataFrame]]]] = {}
    for p in packets(con):
        tables = packet_tables(con, p, per, peer)
        written.append(write_packet(out_dir, p, tables, per))
        by_tenant.setdefault(p.tenant_code, []).append((p, tables))
    for members in by_tenant.values():
        write_program(out_dir, members[0][0], program_tables(con, members, per), per)
    log.info("dealer packets: %d, program packets: %d -> %s", len(written), len(by_tenant), out_dir)
    return written
