"""Read the actuarial inputs from the ``core`` star schema (marts only, never raw or staging).

Every frame is already reportable (demo dealers and the test-ID range are excluded once, upstream in
``int_reportable_scope``). Claims money comes from the one claims fact, ``core.fct_claim_payment_line``.

contracts: contract_id, contract_number, source_system, dealer_number, category_code, subcategory_code,
           product_code (subcategory-term, the V1 product code the report shell uses), new_used,
           term_months, term_miles, claim_wait_days, effective_date, cancel_date (NaT if in force),
           premium (claims-reserve portion of dealer cost), odometer, cancel_odometer
claims:    claim_id, contract_id, loss_date, reported_date, odometer_at_loss, status_group
lines:     claim_id, contract_id, posted_date, paid, incurred (= paid + change in case reserve),
           source_system (the system that posted the line; None when the fact has no such column)

Claims scope: the same validity rule as the Claims marts (``mart_claim_summary.is_contract_matched``):
a claim counts only when it has a contract and its loss date is on or after the contract's effective
date. The rest (no contract, or a loss before the sale) and their payment lines are set aside in
``Inputs.excluded_claims`` / ``Inputs.excluded_lines`` with a ``reason`` column, so ``run_summary``
can carry them as reconciling lines and actuarial "reported" ties to the Claims pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import duckdb
import numpy as np
import pandas as pd

log = logging.getLogger("actuarial")

REQUIRED = (
    "core.dim_contract",
    "core.dim_contract_terms",
    "core.dim_contract_finance",
    "core.dim_contract_vehicle",
    "core.dim_claim",
    "core.fct_claim_payment_line",
    "core.fct_cancellation",
)

CONTRACTS_SQL = """
    select c.contract_key as contract_id, c.contract_number, c.source_system, c.dealer_number,
           c.category_code, c.subcategory_code,
           c.subcategory_code || '-' || cast(t.term_months as varchar) as product_code,
           upper(v.new_used) as new_used, t.term_months, t.term_miles, t.claim_wait_days,
           c.effective_date, case when c.is_cancelled then c.cancel_date end as cancel_date,
           coalesce(f.reserve_amount, 0) as premium, v.odometer, x.cancel_odometer
    from core.dim_contract c
    join core.dim_contract_terms t using (contract_key)
    left join core.dim_contract_finance f using (contract_key)
    left join core.dim_contract_vehicle v using (contract_key)
    left join (
        select contract_key, max(odometer) as cancel_odometer
        from core.fct_cancellation where is_effective_cancel group by 1
    ) x using (contract_key)
    where c.contract_key <> '-1' and t.term_months > 0 and c.effective_date is not null
"""

CLAIMS_SQL = """
    select claim_key as claim_id, contract_key as contract_id, loss_date,
           coalesce(reported_date, loss_date) as reported_date, odometer_at_loss, status_group
    from core.dim_claim
"""


LINES_SQL = """
    select claim_key as claim_id, contract_key as contract_id, cast(posted_at as date) as posted_date,
           coalesce(paid_amount, 0) as paid, coalesce(incurred_change, 0) as incurred, {source} as source_system
    from core.fct_claim_payment_line
"""


@dataclass(frozen=True)
class Inputs:
    contracts: pd.DataFrame
    claims: pd.DataFrame
    lines: pd.DataFrame
    source: str = "core"
    # claims outside the reportable scope (and their lines), with a ``reason``: no_contract / loss_before_sale
    excluded_claims: pd.DataFrame = field(default_factory=pd.DataFrame)
    excluded_lines: pd.DataFrame = field(default_factory=pd.DataFrame)


def missing_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    have = {
        f"{s}.{t}"
        for s, t in con.execute("select table_schema, table_name from information_schema.tables").fetchall()
    }
    return [t for t in REQUIRED if t not in have]


def _dates(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        df[c] = pd.to_datetime(df[c])
    return df


def load_inputs(con: duckdb.DuckDBPyConnection) -> Inputs:
    missing = missing_tables(con)
    if missing:
        raise RuntimeError(f"core marts missing ({', '.join(missing)}); run dbt build first")
    contracts = _dates(con.execute(CONTRACTS_SQL).df(), ["effective_date", "cancel_date"])
    claims = _dates(con.execute(CLAIMS_SQL).df(), ["loss_date", "reported_date"])
    has_source = con.execute(
        "select count(*) from information_schema.columns where table_schema = 'core' "
        "and table_name = 'fct_claim_payment_line' and column_name = 'source_system'").fetchone()[0]
    sql = LINES_SQL.format(source="source_system" if has_source else "cast(null as varchar)")
    lines = _dates(con.execute(sql).df(), ["posted_date"])
    claims.loc[claims["contract_id"] == "-1", "contract_id"] = None  # '-1' is the unknown member
    lines.loc[lines["contract_id"] == "-1", "contract_id"] = None
    return scope_claims(Inputs(contracts, claims, lines))


def scope_claims(inputs: Inputs) -> Inputs:
    """Split out claims (and lines) that fail the Claims-mart validity rule; see the module docstring.

    A claim needs a contract in ``inputs.contracts`` and a loss on or after its effective date. A line
    goes with its claim; a line with no contract or no in-scope claim is ``no_contract``.
    """
    claims, lines = inputs.claims, inputs.lines
    eff = claims["contract_id"].map(inputs.contracts.set_index("contract_id")["effective_date"])
    reason = pd.Series(np.select([eff.isna(), claims["loss_date"].isna() | (claims["loss_date"] < eff)],
                                 ["no_contract", "loss_before_sale"], ""), index=claims.index)
    bad = reason != ""
    by_claim = pd.Series(reason[bad].to_numpy(), index=claims.loc[bad, "claim_id"].to_numpy())
    line_reason = lines["claim_id"].map(by_claim).fillna("")
    orphan = lines["contract_id"].isna() | ~lines["claim_id"].isin(claims["claim_id"])
    line_reason = line_reason.mask((line_reason == "") & orphan, "no_contract")
    lbad = line_reason != ""
    if bad.any() or lbad.any():
        log.info("claims scope: %d claims / %d lines set aside (no contract or loss before the sale)",
                 int(bad.sum()), int(lbad.sum()))
    return Inputs(
        inputs.contracts, claims[~bad].reset_index(drop=True), lines[~lbad].reset_index(drop=True), inputs.source,
        excluded_claims=claims[bad].assign(reason=reason[bad]).reset_index(drop=True),
        excluded_lines=lines[lbad].assign(reason=line_reason[lbad]).reset_index(drop=True),
    )
