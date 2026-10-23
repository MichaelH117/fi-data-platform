"""Read the regulatory marts (schema ``regulatory``) and the seeds they are built from.

Reports read marts only. Every query here names its grain; the counting rules themselves live in
``dbt/seeds/regulatory`` and were applied by dbt, so nothing in this file filters statuses.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pandas as pd

CONTRACT_COLS = {
    "state": "State", "provider_name": "Provider", "provider_basis": "Provider basis",
    "dealer_number": "Dealer", "dealer_name": "Dealer name", "insurer_name": "Insurer",
    "contract_number": "Contract no", "status": "Status", "category": "Category", "subcategory": "Subcategory",
    "product": "Product", "entered_date": "Entered", "effective_date": "Effective",
    "expiration_date": "Expiration", "cancel_date": "Cancelled", "coverage_cost": "Coverage cost",
    "retail_price": "Retail price", "coverage_cost_refund": "Coverage cost refund", "reserve_amount": "Reserve",
    "is_in_force_at_period_end": "In force at period end", "contract_count": "Contract count",
}
CLAIM_COLS = {
    "state": "State", "provider_name": "Provider", "dealer_number": "Dealer", "insurer_name": "Insurer",
    "contract_number": "Contract no", "claim_number": "Claim no", "category": "Category",
    "subcategory": "Subcategory", "product": "Product", "reported_date": "Created", "closed_date": "Closed",
    "status": "Status", "substatus": "Substatus", "paid": "Claims paid", "claim_count": "Claim count",
}
CANCEL_COLS = {
    "state": "State", "provider_name": "Provider", "contract_number": "Contract no", "cancel_date": "Cancel date",
    "reason": "Reason", "method": "Method", "initiated_by": "Initiated by", "refund_pct": "Refund %",
    "customer_refund": "Customer refund", "coverage_cost_refund": "Coverage cost refund", "cancel_fee": "Cancel fee",
}


@dataclass
class RegulatoryData:
    as_of: pd.Timestamp
    methods: pd.DataFrame  # seeds.state_reserve_method, one row per state (51)
    status_sets: pd.DataFrame
    definitions: pd.DataFrame
    summary: pd.DataFrame  # regulatory.mart_funded_reserve_summary
    survey: pd.DataFrame  # regulatory.mart_market_survey


def _df(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> pd.DataFrame:
    return con.execute(sql, params or []).df()


def available(con: duckdb.DuckDBPyConnection) -> bool:
    """True when the regulatory marts exist in this warehouse (the plug-in skips quietly otherwise)."""
    n = con.execute(
        "select count(*) from information_schema.tables where table_schema = 'regulatory' "
        "and table_name in ('mart_funded_reserve_summary', 'mart_market_survey')"
    ).fetchone()[0]
    return n == 2


def load(con: duckdb.DuckDBPyConnection) -> RegulatoryData:
    as_of = pd.Timestamp(con.execute("select as_of_date from core.dim_reporting_date").fetchone()[0])
    return RegulatoryData(
        as_of=as_of,
        methods=_df(con, "select * from seeds.state_reserve_method order by state"),
        status_sets=_df(con, "select * from seeds.report_status_sets order by status_set_id, status"),
        definitions=_df(con, "select * from seeds.report_definitions "
                                 "order by report_id, section, line_no, scope, basis"),
        summary=_df(con, "select * exclude (_built_at) from regulatory.mart_funded_reserve_summary "
                         "order by state, provider_name, scope, basis"),
        survey=_df(con, "select * exclude (_built_at) from regulatory.mart_market_survey "
                        "order by state, provider_name, item_no, calendar_year"),
    )


def filing_contracts(con: duckdb.DuckDBPyConnection, state: str, scope: str) -> pd.DataFrame:
    """Contract tab of a filing (grain: contract). ITD = written to the period end; PERIOD = in the year."""
    flag = "is_written_itd" if scope == "ITD" else "is_written_in_period"
    cols = ", ".join(CONTRACT_COLS)
    return _df(con, f"select {cols} from regulatory.mart_funded_reserve_contracts "
                    f"where state = ? and is_filing_product_scope and {flag} "
                    "order by provider_name, entered_date, contract_number", [state])


def filing_claims(con: duckdb.DuckDBPyConnection, state: str, scope: str) -> pd.DataFrame:
    """Claim tab of a filing (grain: claim): reported (ITD) or reported/closed in the year (PERIOD)."""
    if scope == "ITD":
        where, paid = "is_reported_itd", "paid_itd"
    else:
        where, paid = "(is_reported_in_period or closed_date between period_start and period_end)", "paid_in_period"
    cols = ", ".join(f"{paid} as paid" if c == "paid" else c for c in CLAIM_COLS)
    return _df(con, f"select {cols} from regulatory.mart_funded_reserve_claims "
                    f"where state = ? and is_filing_product_scope and {where} "
                    "order by provider_name, reported_date, claim_number", [state])


def survey_details(con: duckdb.DuckDBPyConnection, state: str, start, end) -> dict[str, pd.DataFrame]:
    """Market survey detail tabs (contracts issued, cancels, claims reported) for one state's survey years."""
    p = [state, start, end]
    contracts = _df(con, f"select {', '.join(CONTRACT_COLS)} from regulatory.mart_funded_reserve_contracts "
                         "where state = ? and entered_date between ? and ? order by entered_date, contract_number", p)
    from_contract = ("state", "provider_name", "contract_number")
    cancel_cols = ", ".join(("c." if k in from_contract else "x.") + k for k in CANCEL_COLS)
    cancels = _df(con, f"select {cancel_cols} from core.fct_cancellation as x "
                       "inner join regulatory.mart_funded_reserve_contracts as c using (contract_key) "
                       "where c.state = ? and x.cancel_date between ? and ? order by x.cancel_date", p)
    claims = _df(con, f"select {', '.join('paid_to_date as paid' if c == 'paid' else c for c in CLAIM_COLS)} "
                      "from regulatory.mart_funded_reserve_claims "
                      "where state = ? and reported_date between ? and ? order by reported_date, claim_number", p)
    return {"Contracts": contracts.rename(columns=CONTRACT_COLS), "Cancels": cancels.rename(columns=CANCEL_COLS),
            "Claims": claims.rename(columns=CLAIM_COLS)}

# report_definitions.line_item -> mart_funded_reserve_summary column (labels come from the seed).
SUMMARY_COLUMN = {
    "IN_FORCE": "contracts_in_force", "NEW_WRITTEN": "new_contracts_written",
    "COMPLAINTS": "complaints_received", "CLAIMS_PAID_COUNT": "claims_paid_count",
    "CLAIMS_DENIED": "claims_denied_count", "CLAIMS_PAID_AMOUNT_ALL": "claims_paid_amount_all",
    "GROSS_CONSIDERATION": "gross_consideration",
    "CLAIMS_PAID_AMOUNT": "claims_paid_amount", "NET_CONSIDERATION": "net_consideration",
    "CALCULATED_RESERVE": "calculated_reserve", "SECURITY_DEPOSIT": "security_deposit",
    "CURRENT_RESERVE": "current_reserve", "RESERVE_SHORTFALL": "reserve_shortfall",
}
COUNT_LINES = {"IN_FORCE", "NEW_WRITTEN", "COMPLAINTS", "CLAIMS_PAID_COUNT", "CLAIMS_DENIED"}


def filing_lines(definitions: pd.DataFrame, section: int) -> list[tuple[int, str, str]]:
    """(line_no, line_item, label) of one FUNDED_RESERVE section, in filing order."""
    d = definitions[(definitions.report_id == "FUNDED_RESERVE") & (definitions.section == section)]
    d = d.drop_duplicates("line_item").sort_values("line_no")
    return [(int(r.line_no), r.line_item, r.label) for r in d.itertuples()]


def line_notes(definitions: pd.DataFrame) -> dict[str, str]:
    """FUNDED_RESERVE line_item -> per-line note from the seed's notes column.

    One text when the ITD and period notes agree, otherwise both, labelled. The note says which
    population a line covers, so a count and an amount on different populations are explained
    next to the numbers instead of only in the definitions page.
    """
    d = definitions[definitions.report_id == "FUNDED_RESERVE"]
    out: dict[str, str] = {}
    for item, g in d.groupby("line_item", sort=False):
        by_scope = {s: str(n) for s, n in g.drop_duplicates("scope")[["scope", "notes"]].itertuples(index=False)
                    if isinstance(n, str) and n}
        texts = list(dict.fromkeys(by_scope.values()))
        out[item] = texts[0] if len(texts) == 1 else " ".join(
            f"{'ITD' if s == 'ITD' else 'Period'}: {n}" for s, n in sorted(by_scope.items()))
    return out


def survey_notes(definitions: pd.DataFrame) -> dict[str, str]:
    """MARKET_SURVEY line_item -> note from the seed (what population and dollars a survey item covers)."""
    d = definitions[definitions.report_id == "MARKET_SURVEY"].drop_duplicates("line_item")
    return {r.line_item: str(r.notes) for r in d.itertuples() if isinstance(r.notes, str) and r.notes}


def split_notes(rows: list[pd.Series]) -> tuple[list[str], list[tuple[str, str, list[str]]]]:
    """Collapse the methodology notes of a provider's filing rows (scope x basis).

    Returns (notes shared by every row, [(scope, basis, notes only that row has)]). Rows with no
    difference are left out, so the shared paragraph is printed once instead of once per row.
    """
    parts = [[p.strip() for p in str(r.notes).split("; ") if p.strip()] for r in rows]
    common = [p for p in parts[0] if all(p in q for q in parts[1:])] if parts else []
    diffs = [(str(r.scope), str(r.basis), [p for p in q if p not in common]) for r, q in zip(rows, parts, strict=True)]
    return common, [x for x in diffs if x[2]]
