"""DDL for the mock TPA admin source database (SQLite).

The source is deliberately loose, like a legacy admin system export: no primary keys
on the business ids (so duplicate rows can exist) and dates stored as TEXT.
Tables prefixed with ``_gen`` hold generator state and are not part of the feed.
"""

from __future__ import annotations

import sqlite3

FEED_TABLES = ["dealers", "agents", "products", "contracts", "claims", "cancellations"]

DDL = """
CREATE TABLE agents (
    agent_id        TEXT NOT NULL,
    agent_name      TEXT,
    territory       TEXT,
    email           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE dealers (
    dealer_code         TEXT NOT NULL,
    dealer_name         TEXT,
    city                TEXT,
    state               TEXT,
    agent_id            TEXT,
    reinsurance_company TEXT,
    status              TEXT,
    onboarded_date      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE products (
    product_code    TEXT NOT NULL,
    coverage_tier   TEXT,
    term_months     INTEGER,
    term_miles      INTEGER,
    deductible      REAL,
    admin_fee       REAL,
    reserve_amount  REAL,
    commission      REAL,
    suggested_retail REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE contracts (
    contract_id         TEXT NOT NULL,
    dealer_code         TEXT,
    agent_id            TEXT,
    product_code        TEXT,
    sale_date           TEXT,
    effective_date      TEXT,
    expiration_date     TEXT,
    customer_first_name TEXT,
    customer_last_name  TEXT,
    customer_email      TEXT,
    customer_phone      TEXT,
    customer_zip        TEXT,
    vin                 TEXT,
    vehicle_make        TEXT,
    vehicle_model       TEXT,
    vehicle_year        INTEGER,
    new_used            TEXT,
    odometer_at_sale    INTEGER,
    retail_price        REAL,
    dealer_cost         REAL,
    admin_fee           REAL,
    reserve_amount      REAL,
    commission          REAL,
    status              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE claims (
    claim_id            TEXT NOT NULL,
    contract_id         TEXT,
    loss_date           TEXT,
    reported_date       TEXT,
    paid_date           TEXT,
    odometer_at_loss    INTEGER,
    repair_category     TEXT,
    amount_requested    REAL,
    amount_paid         REAL,
    status              TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE cancellations (
    cancellation_id     TEXT NOT NULL,
    contract_id         TEXT,
    cancel_date         TEXT,
    cancel_reason       TEXT,
    odometer_at_cancel  INTEGER,
    refund_amount       REAL,
    cancel_fee          REAL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE TABLE _gen_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INDEXES = [f"CREATE INDEX ix_{t}_updated_at ON {t} (updated_at)" for t in FEED_TABLES] + [
    "CREATE INDEX ix_contracts_id ON contracts (contract_id)",
    "CREATE INDEX ix_claims_id ON claims (claim_id)",
]


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    for stmt in INDEXES:
        conn.execute(stmt)
