"""Build the aggregate-only peer benchmark tables from the core star schema.

Grain: state x coverage tier x term band (each with an ``ALL`` margin) x period.
Population: VSC contracts (coverage tier = VSC subcategory, powertrain to exclusionary) with earned
exposure in the period, sold by a known dealer.
Measures are the same governed formulas as the marts (docs/METRICS.md):
loss ratio = incurred / earned premium, frequency = claim count / earned contract-years.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import duckdb
import pandas as pd

from publish.suppression import ALL, Rules, apply_suppression

DIMS = ("state", "coverage_tier", "term_band")
TERM_BAND_SQL = "case when t.term_months <= 36 then '24-36' when t.term_months <= 48 then '48' else '60-72' end"

# Dealer x cell contributions for every grouping set of the three dimensions.
# ``{period}`` is a SQL expression for the period label, ``{window}`` a filter on m.month_start.
_CONTRIBUTIONS_SQL = f"""
with base as (
    select
        {{period}} as period,
        d.dealer_number as dealer_code,
        d.state,
        p.subcategory as coverage_tier,
        {TERM_BAND_SQL} as term_band,
        m.contract_key as contract_id,
        m.earned_premium,
        m.incurred_claims,
        m.claim_count,
        m.exposure_years
    from core.fct_contract_monthly m
    join core.dim_dealer d on d.dealer_key = m.dealer_key
    join core.dim_product p on p.product_key = m.product_key
    join core.dim_contract_terms t on t.contract_key = m.contract_key
    where d.dealer_key <> '-1' and d.state is not null and p.category_code = 'VSC'
      and m.exposure_years > 0 and {{window}}
)
select
    period,
    dealer_code,
    case when grouping(state) = 1 then '{ALL}' else state end as state,
    case when grouping(coverage_tier) = 1 then '{ALL}' else coverage_tier end as coverage_tier,
    case when grouping(term_band) = 1 then '{ALL}' else term_band end as term_band,
    count(distinct contract_id) as contracts,
    sum(earned_premium) as earned_premium,
    sum(incurred_claims) as incurred_claims,
    sum(claim_count) as claim_count,
    sum(exposure_years) as exposure_years
from base
group by period, dealer_code, cube(state, coverage_tier, term_band)
"""


@dataclass(frozen=True)
class Window:
    last_month: date  # last complete calendar month published (benchmarks are lagged)
    t12_start: date


def benchmark_window(con: duckdb.DuckDBPyConnection) -> Window:
    """Publish complete months only: the month before the as-of month."""
    last, t12 = con.execute(
        """
        select date_trunc('month', as_of_date) - interval 1 month,
               date_trunc('month', as_of_date) - interval 12 month
        from core.dim_reporting_date
        """
    ).fetchone()
    return Window(pd.Timestamp(last).date(), pd.Timestamp(t12).date())


def cell_totals(contrib: pd.DataFrame) -> pd.DataFrame:
    """Collapse dealer contributions to cells, keeping what the suppression rules need."""
    keys = ["period", *DIMS]
    g = contrib.groupby(keys, sort=True)
    cells = g[["contracts", "earned_premium", "incurred_claims", "claim_count", "exposure_years"]].sum()
    cells["dealers"] = g["dealer_code"].nunique()
    share_contracts = g["contracts"].max() / cells["contracts"]
    share_premium = (g["earned_premium"].max() / cells["earned_premium"]).fillna(0.0)
    cells["max_dealer_share"] = pd.concat([share_contracts, share_premium], axis=1).max(axis=1)
    cells = cells.reset_index()
    cells["loss_ratio"] = (cells["incurred_claims"] / cells["earned_premium"]).where(cells["earned_premium"] > 0)
    cells["claim_frequency"] = (cells["claim_count"] / cells["exposure_years"]).where(cells["exposure_years"] > 0)
    cells["contracts"] = cells["contracts"].astype("int64")
    cells["claim_count"] = cells["claim_count"].astype("int64")
    return cells


def _round(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ("earned_premium", "incurred_claims"):
        df[c] = df[c].round(0)
    for c in ("exposure_years",):
        df[c] = df[c].round(1)
    for c in ("loss_ratio", "claim_frequency"):
        df[c] = df[c].round(4)
    return df


def build_benchmarks(con: duckdb.DuckDBPyConnection, rules: Rules = Rules()) -> dict[str, pd.DataFrame]:
    w = benchmark_window(con)
    monthly_sql = _CONTRIBUTIONS_SQL.format(
        period="strftime(m.month_start, '%Y-%m')", window=f"m.month_start <= date '{w.last_month}'"
    )
    t12_sql = _CONTRIBUTIONS_SQL.format(
        period=f"'T12 to {w.last_month:%Y-%m}'",
        window=f"m.month_start between date '{w.t12_start}' and date '{w.last_month}'",
    )
    out = {}
    for name, sql in (("peer_benchmark_monthly", monthly_sql), ("peer_benchmark_t12", t12_sql)):
        cells = cell_totals(con.execute(sql).df())
        published = apply_suppression(_round(cells), DIMS, "period", rules)
        out[name] = published.rename(columns={"period": "month" if name.endswith("monthly") else "window"})
    return out
