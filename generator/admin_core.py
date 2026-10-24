"""Admin Core: the operational (OLTP) admin system. SQLite here, standing in for SQL Server.

Schema rules
- Multi-tenant: every business table carries ``tenant_id`` (white-label administrator).
- Change tracking: one global counter in ``_ct_version``. Every insert or update stamps the
  row's ``row_version`` with a fresh value, so the replicator can pull "everything after
  version N" (the SQL Server Change Tracking pattern).
- Soft deletes: rows are never hard deleted. ``removed_from_source = 1`` marks a delete and
  also bumps ``row_version`` so the delete replicates.
- Every business table has ``created_at``, ``updated_at``, ``row_version``, ``removed_from_source``.

All writes go through :func:`insert` and :func:`update` so the rules above always hold.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence

import pandas as pd

# table -> (primary key, column DDL). Standard audit columns are appended automatically.
TABLES: dict[str, tuple[str, str]] = {
    # ---- tenancy / white-label
    "administrator": ("tenant_id", """
        tenant_id INTEGER NOT NULL, administrator_code TEXT NOT NULL UNIQUE, reportable_name TEXT NOT NULL,
        logo_path TEXT, primary_color TEXT, secondary_color TEXT"""),
    # ---- parties
    "dealer_group": ("dealer_group_id", """
        dealer_group_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, group_code TEXT NOT NULL, group_name TEXT"""),
    "agent": ("agent_id", """
        agent_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, agent_code TEXT NOT NULL, agent_name TEXT,
        territory TEXT, email TEXT, is_advance_agreement INTEGER NOT NULL DEFAULT 0"""),
    "dealer": ("dealer_id", """
        dealer_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, dealer_number TEXT NOT NULL, dealer_name TEXT,
        dealer_group_id INTEGER, agent_id INTEGER, city TEXT, state TEXT NOT NULL, status TEXT NOT NULL,
        onboarded_date DATE, is_demo INTEGER NOT NULL DEFAULT 0, remit_rule TEXT NOT NULL,
        weekly_remit_flag INTEGER NOT NULL DEFAULT 0, program TEXT NOT NULL DEFAULT 'RETAIL'"""),
    "lienholder": ("lienholder_id", """
        lienholder_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, lienholder_code TEXT NOT NULL,
        lienholder_name TEXT"""),
    "account_holder": ("account_holder_id", """
        account_holder_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, holder_type TEXT NOT NULL,
        holder_name TEXT, state TEXT, ref_id INTEGER"""),
    # ---- risk entities
    "obligor": ("obligor_id", """
        obligor_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, obligor_name TEXT NOT NULL, state TEXT"""),
    "insurer": ("insurer_id", """
        insurer_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, insurer_name TEXT NOT NULL"""),
    "reinsurer": ("reinsurer_id", """
        reinsurer_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, reinsurer_name TEXT NOT NULL"""),
    "participation_entity": ("entity_id", """
        entity_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, dealer_id INTEGER NOT NULL, entity_name TEXT,
        structure_type TEXT NOT NULL, reinsurer_id INTEGER, effective_date DATE, profit_share_pct REAL"""),
    # ---- products / rating
    "product_category": ("category_id", """
        category_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, category_code TEXT NOT NULL, category_name TEXT"""),
    "product_subcategory": ("subcategory_id", """
        subcategory_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, category_id INTEGER NOT NULL,
        subcategory_code TEXT NOT NULL, subcategory_name TEXT"""),
    "product": ("product_id", """
        product_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, subcategory_id INTEGER NOT NULL,
        product_code TEXT NOT NULL, product_name TEXT, contract_prefix TEXT NOT NULL, plan_year INTEGER NOT NULL,
        is_cancellable INTEGER NOT NULL, claim_wait_days INTEGER, claim_wait_miles INTEGER, coverage_limit REAL"""),
    "plan": ("plan_id", """
        plan_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, product_id INTEGER NOT NULL, plan_code TEXT NOT NULL,
        plan_name TEXT, deductible_amount REAL, deductible_name TEXT"""),
    "program": ("program_id", """
        program_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, plan_id INTEGER NOT NULL,
        program_code TEXT NOT NULL, program_name TEXT"""),
    "rate": ("rate_id", """
        rate_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, plan_id INTEGER NOT NULL, term_months INTEGER NOT NULL,
        term_miles INTEGER, odometer_band TEXT, class_code TEXT, deductible_amount REAL, coverage_cost REAL NOT NULL,
        suggested_retail REAL"""),
    "rate_component": ("rate_component_id", """
        rate_component_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, rate_id INTEGER NOT NULL,
        disbursement_type TEXT NOT NULL, account_holder_id INTEGER, amount REAL NOT NULL,
        is_refundable INTEGER NOT NULL, is_renewable INTEGER NOT NULL"""),
    # ---- contracts
    "contract": ("contract_id", """
        contract_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_number TEXT NOT NULL UNIQUE,
        deal_id TEXT, dealer_id INTEGER NOT NULL, agent_id INTEGER, plan_id INTEGER NOT NULL,
        program_id INTEGER, rate_id INTEGER NOT NULL, status TEXT NOT NULL, entered_date DATE NOT NULL,
        effective_date DATE NOT NULL, expiration_date DATE NOT NULL, expiration_mileage INTEGER,
        term_months INTEGER NOT NULL, term_miles INTEGER, odometer_band TEXT, deductible_amount REAL,
        deductible_name TEXT, claim_wait_days INTEGER, claim_wait_miles INTEGER, coverage_limit REAL,
        purchase_price REAL NOT NULL, coverage_cost REAL NOT NULL, retail_price REAL, msrp REAL,
        finance_type TEXT, finance_amount REAL, apr REAL, new_used TEXT, odometer INTEGER, vin TEXT,
        chassis_vin TEXT, vehicle_year INTEGER, vehicle_make TEXT, vehicle_model TEXT, fuel_type TEXT,
        cylinders INTEGER, drive_type TEXT, is_commercial INTEGER, title_brand TEXT, class_code TEXT,
        reportable_state TEXT NOT NULL, original_obligor_id INTEGER, current_obligor_id INTEGER,
        insurer_id INTEGER, lienholder_id INTEGER, customer_first_name TEXT, customer_last_name TEXT,
        customer_email TEXT, customer_phone TEXT, customer_zip TEXT, customer_state TEXT"""),
    "contract_surcharge": ("surcharge_id", """
        surcharge_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER NOT NULL,
        surcharge_code TEXT NOT NULL, amount REAL NOT NULL"""),
    "contract_option": ("option_id", """
        option_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER NOT NULL,
        option_code TEXT NOT NULL, amount REAL NOT NULL"""),
    "contract_status_history": ("status_history_id", """
        status_history_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER NOT NULL,
        changed_at TIMESTAMP NOT NULL, from_status TEXT, to_status TEXT NOT NULL"""),
    # ---- amendments / renewals
    "contract_amendment": ("amendment_id", """
        amendment_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER,
        legacy_contract_ref INTEGER, amendment_type TEXT NOT NULL, effective_date DATE NOT NULL,
        delta_coverage_cost REAL NOT NULL DEFAULT 0, note TEXT"""),
    # ---- remittance / cash
    "remit_batch": ("remit_batch_id", """
        remit_batch_id TEXT NOT NULL, tenant_id INTEGER NOT NULL, dealer_id INTEGER NOT NULL,
        entered_at TIMESTAMP NOT NULL, sent_at TIMESTAMP, received_at TIMESTAMP, status TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'NETWORK'"""),
    "cash_transaction": ("cash_txn_id", """
        cash_txn_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, remit_batch_id TEXT NOT NULL,
        contract_id INTEGER NOT NULL, amount REAL NOT NULL, posted_at TIMESTAMP NOT NULL"""),
    # ---- accounting
    "transaction": ("txn_id", """
        txn_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER NOT NULL, txn_type TEXT NOT NULL,
        posted_at TIMESTAMP NOT NULL, amount REAL NOT NULL, source_ref TEXT"""),
    "disbursement": ("disbursement_id", """
        disbursement_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, txn_id INTEGER NOT NULL,
        contract_id INTEGER NOT NULL, disbursement_type TEXT NOT NULL, account_holder_id INTEGER NOT NULL,
        amount REAL NOT NULL, posted_at TIMESTAMP NOT NULL, is_refundable INTEGER, is_renewable INTEGER"""),
    # ---- cancellations
    "cancellation": ("cancel_id", """
        cancel_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER NOT NULL,
        cancel_date DATE NOT NULL, processed_at TIMESTAMP NOT NULL, initiated_by TEXT NOT NULL, reason TEXT,
        method TEXT NOT NULL, odometer INTEGER, contract_age_days INTEGER, refund_pct REAL, cancel_fee REAL,
        customer_refund REAL, admin_portion REAL, dealer_portion REAL, coverage_cost_refund REAL,
        dealer_profit_refund REAL, admin_withheld_claims REAL"""),
    "reinstatement": ("reinstatement_id", """
        reinstatement_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, cancel_id INTEGER NOT NULL,
        contract_id INTEGER NOT NULL, reinstated_at TIMESTAMP NOT NULL, status TEXT NOT NULL"""),
    # ---- claims
    "claim": ("claim_id", """
        claim_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, claim_number TEXT NOT NULL,
        contract_id INTEGER, legacy_contract_ref INTEGER, status TEXT NOT NULL, substatus TEXT,
        denied_reason TEXT, denied_category TEXT, repair_facility_id INTEGER, adjuster_user_id INTEGER,
        loss_date DATE NOT NULL, reported_at TIMESTAMP NOT NULL, odometer INTEGER, repair_order TEXT,
        towing_flag INTEGER, goodwill_flag INTEGER, authorization_required INTEGER, requested_amount REAL,
        deductible_amount REAL"""),
    "claim_component": ("claim_component_id", """
        claim_component_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, claim_id INTEGER NOT NULL,
        component_code TEXT NOT NULL, parts_amount REAL, labor_amount REAL, labor_hours REAL"""),
    "claim_payment_line": ("line_id", """
        line_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, claim_id INTEGER NOT NULL,
        posted_at TIMESTAMP NOT NULL, line_type TEXT NOT NULL, authorized_change REAL NOT NULL DEFAULT 0,
        inspection_change REAL NOT NULL DEFAULT 0, paid_amount REAL NOT NULL DEFAULT 0,
        pending_amount REAL NOT NULL DEFAULT 0, payee_account_holder_id INTEGER"""),
    "claim_history": ("claim_history_id", """
        claim_history_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, claim_id INTEGER NOT NULL,
        changed_at TIMESTAMP NOT NULL, from_status TEXT, to_status TEXT NOT NULL, pre_deductible_change REAL,
        post_deductible_change REAL, collected_deductible REAL"""),
    "claim_note": ("note_id", """
        note_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, claim_id INTEGER NOT NULL,
        author_user_id INTEGER, noted_at TIMESTAMP NOT NULL, note_text TEXT"""),
    "complaint": ("complaint_id", """
        complaint_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER, claim_id INTEGER,
        received_date DATE NOT NULL, state TEXT, category TEXT, source TEXT, is_regulator INTEGER,
        resolution TEXT, resolved_date DATE"""),
    # ---- commissions
    "commission": ("commission_id", """
        commission_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER NOT NULL,
        agent_id INTEGER NOT NULL, txn_id INTEGER, posted_at TIMESTAMP NOT NULL, amount REAL NOT NULL,
        share_pct REAL NOT NULL, is_advance INTEGER NOT NULL"""),
    # ---- users / portal
    "admin_user": ("admin_user_id", """
        admin_user_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, user_name TEXT, role TEXT, email TEXT"""),
    "portal_user": ("portal_user_id", """
        portal_user_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, dealer_id INTEGER, role TEXT NOT NULL,
        login TEXT NOT NULL, api_key_hash TEXT"""),
    # ---- custom fields (EAV): specialty-line attributes stored as text (generator/custom_fields.py)
    "custom_field_def": ("field_def_id", """
        field_def_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, field_id INTEGER NOT NULL,
        field_name TEXT NOT NULL, data_type TEXT NOT NULL"""),
    "contract_custom_field": ("custom_field_id", """
        custom_field_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, contract_id INTEGER NOT NULL,
        field_id INTEGER NOT NULL, value_text TEXT"""),
    # ---- intake (dealer-integration network landing, promoted by `python -m ingest.feed`)
    "intake_file": ("intake_file_id", """
        intake_file_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, feed_date DATE NOT NULL,
        sequence_no INTEGER NOT NULL, file_name TEXT NOT NULL, expected_rows INTEGER, actual_rows INTEGER,
        expected_sha256 TEXT, actual_sha256 TEXT, status TEXT NOT NULL, message TEXT, processed_at TIMESTAMP"""),
    "intake_reject": ("reject_id", """
        reject_id INTEGER NOT NULL, tenant_id INTEGER NOT NULL, intake_file_id INTEGER NOT NULL,
        feed_date DATE NOT NULL, file_name TEXT NOT NULL, row_number INTEGER, reject_reason TEXT NOT NULL,
        raw_json TEXT"""),
}
BUSINESS_TABLES = [t for t in TABLES if not t.startswith("intake_")]
AUDIT_COLS = ["created_at", "updated_at", "row_version", "removed_from_source"]
PII_COLUMNS = {
    "contract": ["customer_first_name", "customer_last_name", "customer_email", "customer_phone", "customer_zip",
                 "vin", "chassis_vin"],
    "agent": ["email"],
    "admin_user": ["user_name", "email"],
}


def ddl() -> str:
    parts = ["CREATE TABLE _ct_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);",
             "INSERT INTO _ct_version VALUES (1, 0);",
             "CREATE TABLE _gen_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);"]
    for name, (pk, cols) in TABLES.items():
        parts.append(
            f'CREATE TABLE "{name}" ({cols.strip()},\n'
            "        created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL,"
            " row_version INTEGER NOT NULL, removed_from_source INTEGER NOT NULL DEFAULT 0,"
            f"\n        PRIMARY KEY ({pk}));"
        )
        parts.append(f'CREATE INDEX ix_{name}_rv ON "{name}" (row_version);')
    for t, col in [("contract", "dealer_id"), ("contract", "status"), ("claim", "contract_id"),
                   ("claim", "status"), ("claim_payment_line", "claim_id"), ("cash_transaction", "contract_id"),
                   ("transaction", "contract_id"), ("disbursement", "txn_id"), ("cancellation", "contract_id")]:
        parts.append(f'CREATE INDEX ix_{t}_{col} ON "{t}" ({col});')
    return "\n".join(parts)


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(ddl())


def column_specs() -> dict[str, dict[str, str]]:
    """table -> {column: "TYPE" or "TYPE NOT NULL"}, read back from the DDL (audit columns included).

    This is what ``contracts/admin_core.yml`` declares per column; a test checks the two agree.
    """
    with sqlite3.connect(":memory:") as conn:
        create_schema(conn)
        return {t: {name: f"{str(decl).upper()}{' NOT NULL' if notnull else ''}"
                    for _, name, decl, notnull, *_ in conn.execute(f'PRAGMA table_info("{t}")')}
                for t in TABLES}


def pk(table: str) -> str:
    return TABLES[table][0]


def next_id(conn: sqlite3.Connection, table: str) -> int:
    """Next integer surrogate key for ``table`` (single writer, so max + 1 is safe)."""
    return int(conn.execute(f'SELECT coalesce(max({pk(table)}), 0) + 1 FROM "{table}"').fetchone()[0])


def allocate_versions(conn: sqlite3.Connection, n: int) -> range:
    """Reserve ``n`` change-tracking versions and return them."""
    cur = int(conn.execute("SELECT version FROM _ct_version WHERE id = 1").fetchone()[0])
    conn.execute("UPDATE _ct_version SET version = ? WHERE id = 1", [cur + n])
    return range(cur + 1, cur + n + 1)


def current_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT version FROM _ct_version WHERE id = 1").fetchone()[0])


def columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def insert(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """Insert rows, stamping ``row_version``; ``created_at``/``updated_at`` must be present."""
    if df is None or df.empty:
        return 0
    df = df.copy()
    if "removed_from_source" not in df:
        df["removed_from_source"] = 0
    df["row_version"] = list(allocate_versions(conn, len(df)))
    cols = [c for c in columns(conn, table) if c in df.columns]
    missing = {"created_at", "updated_at"} - set(cols)
    if missing:
        raise ValueError(f"{table}: missing {sorted(missing)}")
    df = df[cols].astype(object).where(df[cols].notna(), None)
    placeholders = ", ".join("?" * len(cols))
    col_sql = ", ".join(f'"{c}"' for c in cols)
    conn.executemany(f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})', df.itertuples(index=False))
    return len(df)


def update(conn: sqlite3.Connection, table: str, keys: Sequence, values: dict[str, Iterable], updated_at) -> int:
    """Update rows by primary key. Each touched row gets ``updated_at`` and a new ``row_version``.

    ``values`` maps column -> one value per key (or a scalar for all keys); ``updated_at`` is a
    scalar or one timestamp per key.
    """
    keys = list(keys)
    if not keys:
        return 0
    n = len(keys)

    def _vec(v) -> list:
        if isinstance(v, str) or not hasattr(v, "__iter__"):
            return [v] * n
        out = list(v)
        if len(out) != n:
            raise ValueError("value length does not match keys")
        return out

    data = {c: _vec(v) for c, v in values.items()}
    data["updated_at"] = _vec(updated_at)
    data["row_version"] = list(allocate_versions(conn, n))
    sets = ", ".join(f'"{c}" = ?' for c in data)
    rows = [tuple(_py(data[c][i]) for c in data) + (_py(keys[i]),) for i in range(n)]
    conn.executemany(f'UPDATE "{table}" SET {sets} WHERE {pk(table)} = ?', rows)
    return n


def soft_delete(conn: sqlite3.Connection, table: str, keys: Sequence, updated_at) -> int:
    return update(conn, table, keys, {"removed_from_source": 1}, updated_at)


def _py(v):
    """numpy scalars -> plain Python for sqlite3."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and v != v:  # NaN
        return None
    return v


def read(conn: sqlite3.Connection, table: str, where: str = "", params: Sequence = ()) -> pd.DataFrame:
    sql = f'SELECT * FROM "{table}"' + (f" WHERE {where}" if where else "")
    return pd.read_sql(sql, conn, params=list(params))
