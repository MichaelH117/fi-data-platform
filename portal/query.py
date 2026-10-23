"""Scoped, read-only queries against Admin Core for the raw-data portal.

A :class:`Scope` is resolved first (tenant + dealer, from CLI arguments or from an authenticated
portal user) and every dataset query filters on it with bound parameters. Soft-deleted rows
(``removed_from_source = 1``) and change-tracking internals are never returned.

Data minimisation: every dataset selects an explicit column allow-list (never ``SELECT *``).
By default (scope ``standard``) the contracts dataset carries no customer contact data: e-mail,
phone and ZIP are dropped, the customer name comes back as initials (``holder_initials``) and the
VIN / chassis VIN masked to their last 6 characters. Internal risk-party ids (obligor, insurer,
lienholder) are not exported. The opt-in ``contact`` scope adds the customer name, e-mail, phone,
ZIP and full VINs, and only a ``DEALER_ADMIN`` portal user may use it, for its own dealer. Every
export writes one row to the portal access log (see :mod:`portal.audit`).
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

HIDDEN = {"row_version", "removed_from_source"}
SCOPES = ("standard", "contact")
CONTACT_ROLES = {"DEALER_ADMIN"}
# Columns only the opt-in contact scope returns (in clear text).
CONTACT_COLUMNS = ["customer_first_name", "customer_last_name", "customer_email", "customer_phone", "customer_zip",
                   "vin", "chassis_vin"]


class AccessDenied(Exception):
    """The caller is not allowed to see the requested scope (message never says why in detail)."""


@dataclass(frozen=True)
class Scope:
    tenant_id: int
    tenant_code: str
    dealer_id: int | None  # None = whole tenant (tenant admin only)
    dealer_number: str | None


# dataset -> SQL. ``{where}`` is replaced by the scope filter on alias c (contract).
CONTRACT_COLUMNS = [
    "contract_id", "tenant_id", "contract_number", "deal_id", "dealer_id", "status", "entered_date", "effective_date",
    "expiration_date", "expiration_mileage", "term_months", "term_miles", "odometer_band", "deductible_amount",
    "deductible_name", "claim_wait_days", "claim_wait_miles", "coverage_limit", "purchase_price", "coverage_cost",
    "retail_price", "msrp", "finance_type", "finance_amount", "apr", "new_used", "odometer", "vehicle_year",
    "vehicle_make", "vehicle_model", "fuel_type", "cylinders", "drive_type", "is_commercial", "title_brand",
    "class_code", "reportable_state", "created_at", "updated_at",
]
DATASETS = {
    "contracts": f"""
        SELECT {", ".join("c." + col for col in CONTRACT_COLUMNS)}, d.dealer_number, pl.plan_code, p.product_code,
               p.plan_year, {", ".join("c." + col for col in CONTACT_COLUMNS)}
        FROM contract c
        JOIN dealer d ON d.dealer_id = c.dealer_id
        JOIN plan pl ON pl.plan_id = c.plan_id
        JOIN product p ON p.product_id = pl.product_id
        WHERE c.removed_from_source = 0 AND {{where}}
        ORDER BY c.contract_id""",
    "claims": """
        SELECT k.claim_id, k.claim_number, c.contract_number, k.status, k.substatus, k.denied_reason, k.loss_date,
               k.reported_at, k.odometer, k.repair_order, k.requested_amount, k.deductible_amount,
               coalesce(sum(l.paid_amount), 0) AS paid_to_date, coalesce(sum(l.pending_amount), 0) AS outstanding,
               k.updated_at
        FROM claim k
        JOIN contract c ON c.contract_id = k.contract_id
        LEFT JOIN claim_payment_line l ON l.claim_id = k.claim_id AND l.removed_from_source = 0
        WHERE k.removed_from_source = 0 AND {where}
        GROUP BY k.claim_id
        ORDER BY k.claim_id""",
    "cancellations": """
        SELECT x.cancel_id, x.tenant_id, x.contract_id, c.contract_number, x.cancel_date, x.processed_at,
               x.initiated_by, x.reason, x.method, x.odometer, x.contract_age_days, x.refund_pct, x.cancel_fee,
               x.customer_refund, x.admin_portion, x.dealer_portion, x.coverage_cost_refund, x.dealer_profit_refund,
               x.admin_withheld_claims, x.created_at, x.updated_at
        FROM cancellation x
        JOIN contract c ON c.contract_id = x.contract_id
        WHERE x.removed_from_source = 0 AND {where}
        ORDER BY x.cancel_id""",
    "remittances": """
        SELECT b.remit_batch_id, b.status AS batch_status, b.entered_at, b.sent_at, b.received_at,
               c.contract_number, t.amount, t.posted_at
        FROM cash_transaction t
        JOIN remit_batch b ON b.remit_batch_id = t.remit_batch_id
        JOIN contract c ON c.contract_id = t.contract_id
        WHERE t.removed_from_source = 0 AND {where}
        ORDER BY b.remit_batch_id, c.contract_number""",
}


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"Admin Core not found: {path}")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)


def _tenant(conn: sqlite3.Connection, tenant: str) -> tuple[int, str]:
    row = conn.execute(
        "SELECT tenant_id, administrator_code FROM administrator WHERE removed_from_source = 0 "
        "AND (administrator_code = ? OR CAST(tenant_id AS TEXT) = ?)", [tenant.upper(), tenant]
    ).fetchone()
    if not row:
        raise AccessDenied("unknown tenant or dealer")
    return int(row[0]), str(row[1])


def resolve(conn: sqlite3.Connection, tenant: str, dealer: str | None) -> Scope:
    """Scope for an operator export (``--tenant`` code or id, ``--dealer`` number)."""
    tenant_id, code = _tenant(conn, tenant)
    if dealer is None:
        return Scope(tenant_id, code, None, None)
    row = conn.execute(
        "SELECT dealer_id, dealer_number FROM dealer WHERE tenant_id = ? AND dealer_number = ? "
        "AND removed_from_source = 0", [tenant_id, dealer.strip().upper()]
    ).fetchone()
    if not row:  # same message whether the dealer is missing or belongs to another tenant
        raise AccessDenied("unknown tenant or dealer")
    return Scope(tenant_id, code, int(row[0]), str(row[1]))


def authenticate(conn: sqlite3.Connection, login: str, api_key: str) -> tuple[Scope, str]:
    """Scope of a portal user, after checking its API key. Returns (scope, role)."""
    row = conn.execute(
        "SELECT u.tenant_id, a.administrator_code, u.dealer_id, d.dealer_number, u.role, u.api_key_hash "
        "FROM portal_user u JOIN administrator a ON a.tenant_id = u.tenant_id "
        "LEFT JOIN dealer d ON d.dealer_id = u.dealer_id "
        "WHERE u.login = ? AND u.removed_from_source = 0", [login]
    ).fetchone()
    given = hashlib.sha256((api_key or "").encode()).hexdigest()
    if not row or not row[5] or not hmac.compare_digest(given, row[5]):
        raise AccessDenied("invalid credentials")
    tenant_id, code, dealer_id, number, role, _ = row
    return Scope(int(tenant_id), code, None if dealer_id is None else int(dealer_id), number), role


def narrow(conn: sqlite3.Connection, scope: Scope, dealer: str | None) -> Scope:
    """A tenant-wide scope may narrow to one of its own dealers; a dealer scope may only name itself."""
    if dealer is None:
        return scope
    if scope.dealer_id is not None:
        if dealer.strip().upper() != scope.dealer_number:
            raise AccessDenied("unknown tenant or dealer")
        return scope
    return resolve(conn, str(scope.tenant_id), dealer)


def check_data_scope(role: str | None, scope: Scope, data_scope: str) -> None:
    """``contact`` is opt-in and only for a DEALER_ADMIN reading its own dealer."""
    if data_scope not in SCOPES:
        raise AccessDenied(f"unknown data scope {data_scope!r}")
    if data_scope == "contact" and (role not in CONTACT_ROLES or scope.dealer_id is None):
        raise AccessDenied("the contact scope needs a dealer admin")


def _initials(first: pd.Series, last: pd.Series) -> pd.Series:
    f = first.fillna("").astype(str).str.strip().str[:1].str.upper()
    lt = last.fillna("").astype(str).str.strip().str[:1].str.upper()
    out = (f + ". " + lt + ".").str.replace(r"^\. |(?<= )\.$", "", regex=True).str.strip()
    return out.where((f != "") | (lt != ""), None)


def _mask_vin(v: pd.Series) -> pd.Series:
    s = v.astype(object)
    return s.map(lambda x: None if x is None or (isinstance(x, float) and x != x) or not str(x).strip()
                 else "*" * max(len(str(x)) - 6, 0) + str(x)[-6:])


def minimise(df: pd.DataFrame, data_scope: str = "standard") -> pd.DataFrame:
    """Apply the data-minimisation rules of a scope to a fetched frame."""
    if data_scope == "contact" or "customer_first_name" not in df.columns:
        return df
    df = df.copy()
    df["holder_initials"] = _initials(df["customer_first_name"], df["customer_last_name"])
    df["vin_masked"] = _mask_vin(df["vin"])
    df["chassis_vin_masked"] = _mask_vin(df["chassis_vin"])
    return df.drop(columns=CONTACT_COLUMNS)


def fetch(conn: sqlite3.Connection, scope: Scope, dataset: str, since: str | None = None,
          data_scope: str = "standard") -> pd.DataFrame:
    """Rows of one dataset for ``scope``. Callers check the role first with :func:`check_data_scope`."""
    if dataset not in DATASETS:
        raise KeyError(f"unknown dataset {dataset!r}; choose from {sorted(DATASETS)}")
    if data_scope not in SCOPES:
        raise AccessDenied(f"unknown data scope {data_scope!r}")
    where, params = "c.tenant_id = ?", [scope.tenant_id]
    if scope.dealer_id is not None:
        where += " AND c.dealer_id = ?"
        params.append(scope.dealer_id)
    if since:
        where += " AND c.updated_at >= ?"
        params.append(since)
    df = pd.read_sql(DATASETS[dataset].format(where=where), conn, params=params)
    return minimise(df[[c for c in df.columns if c not in HIDDEN]], data_scope)
