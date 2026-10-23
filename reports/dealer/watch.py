"""One dealer watch rule for every page (owner: Senior Data Analyst, dealer & claims).

The executive overview, the Dealers page and the dealer scorecard all count and rank watch dealers
from ``dealer.mart_dealer_scorecard`` for the last complete month, with the thresholds and minimum
volumes of seed ``dealer_claim_kpi_rules`` (scorecard_*). Nothing here imports ``reports.build``, so
the shell can call it without an import cycle.

Loss ratio here is the dealer mart's: incurred claims (loss month) over the EARNED CLAIMS RESERVE
(earned month), metric label "Loss ratio on claims reserve".
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

SEED_CSV = Path(__file__).resolve().parents[2] / "dbt" / "seeds" / "dealer" / "dealer_claim_kpi_rules.csv"
LR_LABEL = "Loss ratio on claims reserve"

# One qualified label per cancel-rate variant (metric cancel_rate: cancelled / written, by sale cohort). The
# window is part of the label, so one label always means one number for a dealer on every page: the
# overview, the Dealers page, the dealer scorecard and the per-dealer packet (publish/packets.py uses the
# same strings; tests/test_dealer.py pins them).
CANCEL_RATE_ITD = "Cancel rate, by sale cohort, ITD"
CANCEL_RATE_T12 = "Cancel rate, by sale cohort, T12"
CANCEL_RATE_MONTH = "Cancel rate, by sale cohort, month"


def rules(con: duckdb.DuckDBPyConnection) -> dict[str, float]:
    """scorecard_* rules as {rule_id: value}: the loaded seed, else the seed CSV (same source of truth)."""
    try:
        df = con.execute("select rule_id, rule_value from seeds.dealer_claim_kpi_rules").df()
    except duckdb.Error:
        df = pd.read_csv(SEED_CSV, dtype=str)
    out = {}
    for r in df.itertuples():
        if str(r.rule_id).startswith("scorecard_"):
            out[r.rule_id] = float(r.rule_value)
    return out


def scorecard_month(con: duckdb.DuckDBPyConnection, month) -> pd.DataFrame:
    """Scorecard rows (one per tenant x dealer) for ``month`` (a month start), plus ``packet_path``: the
    dealer's one published deliverable, ``dealers/<tenant_code>/<dealer_number>/index.html``. It comes from
    the same user -> dealer, tenant map publish writes packets from, so it is null when no packet exists."""
    month = pd.Timestamp(month).date()
    try:
        sc = con.execute(
            """
            select s.*, 'dealers/' || a.tenant_code || '/' || s.dealer_number || '/index.html' as packet_path
            from dealer.mart_dealer_scorecard as s
            left join (select distinct tenant_key, dealer_key, tenant_code from core.dim_user_dealer_access) as a
                using (tenant_key, dealer_key)
            where s.month_start = ?
            """, [month]).df()
    except duckdb.CatalogException:  # access map not built: no packets, so no links
        sc = con.execute("select * from dealer.mart_dealer_scorecard where month_start = ?", [month]).df()
        sc["packet_path"] = None
    sc["month_start"] = pd.to_datetime(sc["month_start"])
    return sc


def packet_links(cur: pd.DataFrame | None) -> dict[str, str]:
    """{dealer_number: packet path} for one month of scorecard rows (dealers with a packet only)."""
    if cur is None or "packet_path" not in cur:
        return {}
    has = cur.dropna(subset=["packet_path"])
    return dict(zip(has["dealer_number"], has["packet_path"], strict=True))


def watch_counts(cur: pd.DataFrame) -> dict[str, int]:
    """Dealers scored, on loss-ratio watch and on any watch, for one month of scorecard rows."""
    return {"scored": len(cur), "loss_ratio": int(cur["is_watch_loss_ratio"].sum()),
            "any": int(cur["is_watch"].sum())}


def lr_watchlist(cur: pd.DataFrame, min_earned: float, n: int = 10) -> pd.DataFrame:
    """Top ``n`` dealers by T12 loss ratio among those that meet the minimum T12 earned claims reserve."""
    big = cur[cur["t12_earned_claims_reserve"] >= min_earned]
    return big.sort_values(["t12_loss_ratio", "dealer_number"], ascending=[False, True]).head(n)


def min_volume_text(r: dict[str, float]) -> str:
    """'T12, minimum $10K earned claims reserve' from the seed."""
    m = r.get("scorecard_min_t12_earned", 0.0)
    amount = f"${m / 1000:,.0f}K" if m >= 1000 else f"${m:,.0f}"
    return f"T12, minimum {amount} earned claims reserve"
