"""Admin Core OLTP: DDL rules, change tracking, soft deletes, contract file, booking math."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from generator import admin_core as ac
from generator import catalog as cat
from generator.booking import refund_terms

CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "admin_core.yml"
MODULES = {"administrator", "dealer", "dealer_group", "agent", "account_holder", "participation_entity", "rate",
           "rate_component", "contract", "contract_amendment", "remit_batch", "cash_transaction", "transaction",
           "disbursement", "cancellation", "reinstatement", "claim", "claim_payment_line", "claim_history",
           "commission", "portal_user"}


@pytest.fixture
def conn(tmp_path: Path):
    c = sqlite3.connect(tmp_path / "ac.db")
    ac.create_schema(c)
    yield c
    c.close()


def _admin(n: int = 2) -> pd.DataFrame:
    return pd.DataFrame({
        "tenant_id": range(1, n + 1), "administrator_code": [f"T{i}" for i in range(n)],
        "reportable_name": [f"Tenant {i}" for i in range(n)], "logo_path": None, "primary_color": "#000000",
        "secondary_color": "#FFFFFF", "created_at": "2024-01-01 09:00:00", "updated_at": "2024-01-01 09:00:00"})


def test_every_table_has_pk_tenant_and_audit_columns(conn) -> None:
    assert MODULES <= set(ac.TABLES)
    for t, (pk, _) in ac.TABLES.items():
        info = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
        cols = {r[1] for r in info}
        assert {"tenant_id", *ac.AUDIT_COLS} <= cols, t
        assert [r[1] for r in info if r[5]] == [pk], t


def test_contract_file_matches_ddl() -> None:
    spec = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    assert spec["replication"]["version_column"] == "row_version"
    assert list(spec["tables"]) == list(ac.TABLES)
    for t, (pk, _) in ac.TABLES.items():
        assert spec["tables"][t]["primary_key"] == [pk]
    specs = ac.column_specs()
    for t in ac.TABLES:  # column names, order, types and NOT NULL flags all come from the DDL
        assert spec["tables"][t]["columns"] == specs[t], t
    assert specs["contract"]["entered_date"] == "DATE NOT NULL" and specs["contract"]["vin"] == "TEXT"


def test_insert_and_update_stamp_new_versions(conn) -> None:
    ac.insert(conn, "administrator", _admin())
    v1 = dict(conn.execute("select tenant_id, row_version from administrator"))
    assert sorted(v1.values()) == [1, 2]
    ac.update(conn, "administrator", [2], {"primary_color": "#123456"}, "2024-02-01 10:00:00")
    row = conn.execute(
        "select row_version, updated_at, primary_color from administrator where tenant_id = 2"
    ).fetchone()
    assert row == (3, "2024-02-01 10:00:00", "#123456")
    assert ac.current_version(conn) == 3
    assert conn.execute("select row_version from administrator where tenant_id = 1").fetchone()[0] == 1


def test_soft_delete_keeps_the_row_and_bumps_the_version(conn) -> None:
    ac.insert(conn, "administrator", _admin(1))
    ac.soft_delete(conn, "administrator", [1], "2024-03-01 08:00:00")
    assert conn.execute("select removed_from_source, row_version from administrator").fetchone() == (1, 2)


def test_insert_requires_timestamps(conn) -> None:
    with pytest.raises(ValueError):
        ac.insert(conn, "administrator", _admin().drop(columns=["created_at"]))


def test_tenant_branding_columns_and_three_tenants() -> None:
    cols = ac.TABLES["administrator"][1]
    for c in ("reportable_name", "logo_path", "primary_color", "secondary_color"):
        assert c in cols
    assert len(cat.TENANTS) == 3


def test_refund_terms() -> None:
    cx = pd.DataFrame({"purchase_price": [2000.0, 2000.0, 1200.0], "coverage_cost": [1000.0, 1000.0, 600.0],
                       "admin_fee": [100.0, 100.0, 60.0], "term_months": [48, 48, 12],
                       "contract_age_days": [30, 730, 92], "method": ["FULL_REFUND", "PRORATED", "R78"],
                       "withheld": [0.0, 50.0, 0.0]})
    t = refund_terms(cx)
    assert t["refund_pct"].iloc[0] == 1.0 and t["cancel_fee"].iloc[0] == 0
    assert t["customer_refund"].iloc[0] == pytest.approx(2000 - 100)  # admin fee is not refundable
    assert t["refund_pct"].iloc[1] == pytest.approx(1 - 730 / round(48 * 30.4375), abs=1e-4)
    assert t["admin_withheld_claims"].iloc[1] == 50.0
    k = np.ceil(92 / 30.4375)  # rule of 78 with 4 elapsed months of 12
    assert t["refund_pct"].iloc[2] == pytest.approx((12 - k) * (13 - k) / (12 * 13), abs=1e-4)
    assert (t["customer_refund"] == (t["admin_portion"] + t["dealer_portion"] - t["cancel_fee"]
                                     - t["admin_withheld_claims"]).clip(lower=0).round(2)).all()
