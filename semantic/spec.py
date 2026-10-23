"""Hand-owned model design: tables, relationships, domain scopes, drill fields, RLS and storage.

Measures are NOT defined here: they come from ``dbt/models/metrics.yml`` (see ``semantic.metrics``).
This file only says how the core star schema is laid out in Power BI.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase

# ---------------------------------------------------------------------- tables
PERIOD = "Posting Period"
SECURITY = "Security User Access"
CALC_GROUP = "Time Intelligence"
DRILL = "Drill Level"
MEASURES = "Measures"
REPORTING_DATE = "Reporting Date"
# Hidden calculated bridge (account holder -> dealer, tenant) built from the payee columns of the facts
# in the model. RLS reads it to scope Account Holder to a dealer's own payees; it has no relationships.
PAYEE_ACCESS = "Payee Dealer Access"


@dataclass(frozen=True)
class TableSpec:
    name: str  # Power BI table name
    source: str  # warehouse schema.table (Snowflake ANALYTICS.<SCHEMA>.<TABLE>)
    kind: str  # fact | dim | security
    key: str | None = None  # primary key column for dims
    incremental: bool = False  # incremental refresh policy on posting_period_key
    note: str = ""
    # Filing snapshots (one row per filing window) have no posting period: the calculation group and the
    # Posting Period table do not apply to them; slice them by their own window columns instead.
    period_free: bool = False
    # Columns left out of the Power BI table (fnmatch patterns), except ``keep``: precomputed ratios and
    # rolling sums that would be wrong when aggregated; the measures and the calculation group rebuild them.
    drop: tuple[str, ...] = ()
    keep: tuple[str, ...] = ()


DIMS: dict[str, TableSpec] = {t.name: t for t in [
    TableSpec(PERIOD, "core.dim_posting_period", "dim", "posting_period_key",
              note="Accounting calendar. The only table the calculation group filters."),
    TableSpec("Tenant", "core.dim_tenant", "dim", "tenant_key", note="White-label administrator and branding."),
    TableSpec("Dealer", "core.dim_dealer", "dim", "dealer_key"),
    TableSpec("Dealer Group", "core.dim_dealer_group", "dim", "dealer_group_key"),
    TableSpec("Agent", "core.dim_agent", "dim", "agent_key"),
    TableSpec("Product", "core.dim_product", "dim", "product_key"),
    TableSpec("Contract", "core.dim_contract", "dim", "contract_key",
              note="Contract core only; terms, finance, vehicle, class and claim summary are separate dims."),
    TableSpec("Contract Terms", "core.dim_contract_terms", "dim", "contract_key"),
    TableSpec("Contract Finance", "core.dim_contract_finance", "dim", "contract_key"),
    TableSpec("Contract Vehicle", "core.dim_contract_vehicle", "dim", "contract_key"),
    TableSpec("Contract Actuarial Class", "core.dim_contract_actuarial_class", "dim", "contract_key"),
    TableSpec("Contract Claim Summary", "core.dim_contract_claim_summary", "dim", "contract_key"),
    TableSpec("Claim", "core.dim_claim", "dim", "claim_key"),
    TableSpec("Account Holder", "core.dim_account_holder", "dim", "account_holder_key"),
    TableSpec("Obligor", "core.dim_obligor", "dim", "obligor_key"),
    TableSpec("Participation Entity", "core.dim_participation_entity", "dim", "participation_entity_key"),
    TableSpec(SECURITY, "core.dim_user_dealer_access", "security", "user_key",
              note="Dynamic RLS map user -> tenant, dealer. Hidden and disconnected; roles read it."),
    TableSpec(REPORTING_DATE, "core.dim_reporting_date", "helper",
              note="One row: as-of date and current period. Caps the calculation group at the as-of period."),
]}

# Fact tables: the Power BI name must match config.meta.pbi.table in metrics.yml.
FACTS: dict[str, TableSpec] = {t.name: t for t in [
    TableSpec("Gross Premium", "core.fct_gross_premium", "fact"),
    TableSpec("Cancellation", "core.fct_cancellation", "fact"),
    TableSpec("Reinstatement", "core.fct_reinstatement", "fact"),
    TableSpec("Claim Payment Line", "core.fct_claim_payment_line", "fact", incremental=True),
    TableSpec("Claim History", "core.fct_claim_history", "fact"),
    TableSpec("Cash Transaction", "core.fct_cash_transaction", "fact"),
    TableSpec("Disbursement", "core.fct_disbursement", "fact", incremental=True),
    TableSpec("Earned Disbursement", "core.fct_earned_disbursement", "fact", incremental=True,
              note="Largest table (contract x type x period); never restated after close, ideal for incremental."),
    TableSpec("Commission", "core.fct_commission", "fact"),
    TableSpec("Contract Month", "core.fct_contract_monthly", "fact", incremental=True),
    TableSpec("Claim Summary", "claims.mart_claim_summary", "fact",
              note="One row per claim from BOTH admin systems (legacy 2019-2022 and admin core). Claim counts, "
                   "denial and severity come from here, by loss month (active) or reported month (inactive)."),
    TableSpec("Dealer Scorecard", "dealer.mart_dealer_scorecard", "fact",
              note="Dealer x month scorecard: monthly additive columns (written, cancelled, earned claims "
                   "reserve, incurred, remittances, days late) and the watch flags. Ratios are measures; the "
                   "trailing 12 months come from the T12M calculation item, never from rolling columns.",
              drop=("t12_*", "cancel_rate", "loss_ratio", "early_claim_rate", "late_remit_rate",
                    "avg_remit_age_days", "avg_days_late"),
              keep=("t12_loss_ratio_peer_pct",)),
    TableSpec("Funded Reserve Filing", "regulatory.mart_funded_reserve_summary", "fact", period_free=True,
              note="Funded-reserve filing lines (state x provider x scope x basis x filing window). The filing "
                   "measures read the default basis and, unless one Scope is sliced, the ITD rows only (each "
                   "state's latest filing), so ITD and PERIOD are never added together."),
    TableSpec("Regulatory Line Value", "regulatory.mart_regulatory_line_values", "fact", period_free=True,
              note="Every filed line value (funded reserve and market survey). Filing Line Value is blank "
                   "until one Report Id, Line Item, Scope and Basis are in context; across windows only a LAST "
                   "line (in force) shows, at its latest window."),
]}

ALL_TABLES: dict[str, TableSpec] = {**DIMS, **FACTS}

# ---------------------------------------------------------------------- relationships
# Fact foreign key column -> dimension table. Facts relate straight to every dim (a star): the contract
# split dims hang off the fact, not off Contract, so there is never more than one filter path.
FACT_FK: dict[str, tuple[str, ...]] = {
    "posting_period_key": (PERIOD,),
    "tenant_key": ("Tenant",),
    "dealer_key": ("Dealer",),
    "product_key": ("Product",),
    "agent_key": ("Agent",),
    "claim_key": ("Claim",),
    "account_holder_key": ("Account Holder",),
    "payee_account_holder_key": ("Account Holder",),
    "provider_key": ("Obligor",),  # filing provider = obligor (so totals by provider match the filings)
    "contract_key": ("Contract", "Contract Terms", "Contract Finance", "Contract Vehicle",
                     "Contract Actuarial Class", "Contract Claim Summary"),
}
# Facts whose period column is not posting_period_key: fact -> (column, Posting Period column). The
# relationship is active; ROLE_PLAYING_PERIODS adds inactive ones a measure turns on with USERELATIONSHIP
# (metrics.yml config.meta.pbi.date_role).
PERIOD_LINKS: dict[str, tuple[str, str]] = {
    "Claim Summary": ("loss_month", "period_start"),
    "Dealer Scorecard": ("month_key", "posting_period_key"),
}
ROLE_PLAYING_PERIODS: dict[str, tuple[tuple[str, str], ...]] = {
    "Claim Summary": (("reported_month", "period_start"),),
}
# Dimension -> dimension (snowflaked on purpose, single path): (from table, from column, to table).
DIM_FK: tuple[tuple[str, str, str], ...] = (
    ("Dealer", "dealer_group_key", "Dealer Group"),
    ("Contract", "current_obligor_key", "Obligor"),
    ("Participation Entity", "dealer_key", "Dealer"),
)

# Which dimensions each domain model carries (only those a fact in the domain relates to are kept).
DOMAIN_DIMS: dict[str, tuple[str, ...]] = {
    "Sales": (PERIOD, "Tenant", "Dealer", "Dealer Group", "Agent", "Product", "Contract", "Contract Terms",
              "Contract Finance", "Contract Vehicle"),
    "Claims": (PERIOD, "Tenant", "Dealer", "Dealer Group", "Product", "Contract", "Contract Vehicle",
               "Contract Actuarial Class", "Contract Claim Summary", "Claim", "Account Holder"),
    "Finance": (PERIOD, "Tenant", "Dealer", "Dealer Group", "Agent", "Product", "Contract", "Contract Finance",
                "Contract Actuarial Class", "Account Holder", "Participation Entity"),
    "Compliance": (PERIOD, "Tenant", "Dealer", "Product", "Contract", "Contract Terms", "Obligor", "Claim"),
}
DOMAIN_PURPOSE: dict[str, str] = {
    "Sales": "Written business, cancellations, in-force book, remittance and commissions by dealer and product.",
    "Claims": "Claims at payment-line grain, status flow, severity and denial, plus accident-month loss ratio.",
    "Finance": "Ledger-grade premium, disbursement, earning, claims and cash by posting period.",
    "Compliance": "State filing and market-survey inputs (in force, written, consideration, claims paid and denied) "
                  "plus the filed funded-reserve and market-survey lines by provider (Obligor).",
}
# External dealer and partner audiences never see the Compliance model (internal only).
DOMAIN_ROLES: dict[str, tuple[str, ...]] = {
    "Sales": ("Internal", "Tenant Partner", "Dealer User"),
    "Claims": ("Internal", "Tenant Partner", "Dealer User"),
    "Finance": ("Internal", "Tenant Partner", "Dealer User"),
    "Compliance": ("Internal",),
}

# ---------------------------------------------------------------------- site headline KPIs
# Every headline KPI tile on the report site -> the metric (measure) that reproduces it, so a Power BI
# author never rebuilds one in report DAX. Time windows (T12, MTD) come from the calculation group.
HEADLINE_KPIS: dict[str, str] = {
    "Written coverage cost, T12": "gross_coverage_cost",
    "Earned premium, T12": "earned_disbursement_amount",
    "Earned claims reserve, T12": "earned_reserve",
    "Loss ratio, T12": "posted_loss_ratio",
    "Combined ratio, T12": "combined_ratio",
    "Contracts in force": "contracts_in_force",
    "Contracts written, T12": "contracts_written",
    "Dealer markup, T12": "dealer_markup_written",
    "Claim frequency, T12": "claim_frequency",
    "Claim severity, T12": "paid_claim_severity",
    "Denial rate, T12": "denial_rate",
    "Claims reported, T12": "claims_reported_loss_month",
    "Open claims": "open_claims",
    "Open claims aged 30+": "open_claims_aged_30_plus",
    "Average turnaround, T12": "avg_claim_turnaround_days",
    "Early-claim rate": "early_claim_rate",
    "Dealers on watch": "dealers_on_watch",
    "Dealers above {threshold} LR": "dealers_on_loss_ratio_watch",
    "Calculated reserve (filing)": "calculated_reserve",
    "Reserve shortfall (filing)": "reserve_shortfall",
    "Complaints received (filing)": "complaints_received",
}
# Headline KPIs deliberately NOT in the model, with the reason (shown on semantic.html).
HEADLINE_KPIS_OUTSIDE: dict[str, str] = {
    "IBNR (actuarial)": "IBNR and ultimate losses are actuarial valuation output (Python, cohort x quarter "
                        "grain, no tenant or dealer key): not a dbt model, so metrics.yml cannot reference it "
                        "and RLS could not secure it. Read it from the actuarial pages.",
}
# Headline KPIs backed by a certified metrics.yml metric on a V1 semantic model the Power BI model does
# not import (label -> metric). The model's period view of the same idea is named in the reason column.
HEADLINE_KPIS_METRIC_ONLY: dict[str, str] = {
    "Cancel rate, by sale cohort, ITD": "cancel_rate",  # the model carries the period view, effective_cancel_rate
}

# ---------------------------------------------------------------------- field parameter (drill path)
# One field parameter drives the row axis of every matrix ("1st level" / "2nd level" picks reuse it).
DRILL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("Tenant", "Tenant", "reportable_name"),
    ("Dealer Group", "Dealer Group", "group_name"),
    ("Dealer", "Dealer", "dealer_name"),
    ("Dealer State", "Dealer", "state"),
    ("Product Category", "Product", "category"),
    ("Product Subcategory", "Product", "subcategory"),
    ("Product", "Product", "product"),
    ("Plan", "Product", "plan"),
    ("Reportable State", "Contract", "reportable_state"),
    ("New / Used", "Contract Vehicle", "new_used"),
    ("Term Band", "Contract Terms", "term_band"),
    ("Rating Cell", "Contract Actuarial Class", "rating_cell"),
    ("Claim Status", "Claim", "status"),
    ("Denied Category", "Claim", "denied_category"),
    ("Obligor", "Obligor", "obligor_name"),
)

# Natural hierarchies (table -> (hierarchy name, levels)).
HIERARCHIES: dict[str, tuple[str, tuple[str, ...]]] = {
    PERIOD: ("Fiscal Calendar", ("fiscal_year", "fiscal_quarter", "period_label")),
    "Product": ("Product Hierarchy", ("category", "subcategory", "product", "plan", "program")),
    "Dealer": ("Dealer Geography", ("state", "city", "dealer_name")),
}

# ---------------------------------------------------------------------- columns
# Columns never exposed (pipeline metadata, raw PII hashes and mutable housekeeping).
DROP_PREFIXES = ("_",)
DROP_COLUMNS = {"customer_key", "updated_at", "valid_from", "is_current"}


def is_dropped(table: TableSpec, column: str) -> bool:
    """Column left out of the Power BI table (pipeline metadata, PII, or the table's own drop list)."""
    if column.startswith(DROP_PREFIXES) or column in DROP_COLUMNS:
        return True
    return column not in table.keep and any(fnmatchcase(column, p) for p in table.drop)


def is_hidden(table: TableSpec, column: str, dtype: str) -> bool:
    """Keys are hidden everywhere; numeric fact columns are hidden (explicit measures only)."""
    if column.endswith("_key") or column.endswith("_id") or column.startswith("source_"):
        return True
    if column.startswith("is_") and column.endswith("_fixed"):
        return True
    if table.kind in ("security", "helper"):
        return True
    return table.kind == "fact" and dtype in ("int64", "double")


DTYPES: dict[str, str] = {
    "VARCHAR": "string", "BIGINT": "int64", "INTEGER": "int64", "HUGEINT": "int64", "SMALLINT": "int64",
    "DOUBLE": "double", "FLOAT": "double", "DECIMAL": "decimal", "BOOLEAN": "boolean", "DATE": "dateTime",
    "TIMESTAMP": "dateTime", "TIMESTAMP WITH TIME ZONE": "dateTime",
}


def tmdl_type(duck_type: str) -> str:
    base = duck_type.split("(")[0].upper()
    return DTYPES.get(base, "string")
