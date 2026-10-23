"""Legacy admin system (SQLite): the historical book entered before the Admin Core cutover.

It is deliberately loose, like an old admin export, and it has a different keyspace from
Admin Core:
- no primary keys on business ids (duplicate rows exist) and dates stored as TEXT;
- integer ``legacy_contract_id`` and zero-padded 7-digit ``claim_no`` ('0000123');
- claim amounts are stored **negative**;
- two ambiguous producer columns: ``producer`` (servicing agent) and ``producer_code``
  (writing agent, differs for reassigned books);
- a stale ``products`` table that stops at an old plan year, so an inner join drops recent vintages;
- lowercase / padded / missing dealer codes, bad VINs, an old MM/DD/YYYY date format,
  claims dated before the sale and a few negative refunds;
- demo dealers and a test contract-id range (>= 99990000).
The legacy system stays the system of record for its contracts' status and cancellations
(run-off). Claims reported after the cutover are worked in Admin Core with ``legacy_contract_ref``.
"""

from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from generator import catalog as cat
from generator import sim
from generator.reference import name_stem

TABLES = ["dealers", "agents", "products", "contracts", "claims", "cancellations"]
STD = """created_at TEXT NOT NULL, updated_at TEXT NOT NULL, removed_from_source INTEGER NOT NULL DEFAULT 0"""
DDL = f"""
CREATE TABLE agents (agent_id TEXT NOT NULL, agent_name TEXT, territory TEXT, email TEXT, {STD});
CREATE TABLE dealers (dealer_code TEXT NOT NULL, dealer_name TEXT, city TEXT, state TEXT, agent_id TEXT,
    reinsurance_company TEXT, status TEXT, onboarded_date TEXT, {STD});
CREATE TABLE products (product_code TEXT NOT NULL, plan_year INTEGER, coverage_tier TEXT, term_months INTEGER,
    term_miles INTEGER, deductible REAL, admin_fee REAL, reserve_amount REAL, commission REAL,
    suggested_retail REAL, {STD});
CREATE TABLE contracts (legacy_contract_id INTEGER NOT NULL, contract_no TEXT, dealer_code TEXT, producer TEXT,
    producer_code TEXT, product_code TEXT, plan_year INTEGER, term_months INTEGER, term_miles INTEGER,
    deductible REAL, sale_date TEXT, effective_date TEXT, expiration_date TEXT, customer_first_name TEXT,
    customer_last_name TEXT, customer_email TEXT, customer_phone TEXT, customer_zip TEXT, customer_state TEXT,
    vin TEXT, vehicle_make TEXT, vehicle_model TEXT, vehicle_year INTEGER, new_used TEXT, odometer_at_sale INTEGER,
    retail_price REAL, dealer_cost REAL, admin_fee REAL, reserve_amount REAL, commission REAL, status TEXT, {STD});
CREATE TABLE claims (claim_no TEXT NOT NULL, legacy_contract_id INTEGER, loss_date TEXT, reported_date TEXT,
    paid_date TEXT, odometer_at_loss INTEGER, repair_category TEXT, amount_requested REAL, amount_paid REAL,
    status TEXT, {STD});
CREATE TABLE cancellations (cancellation_id TEXT NOT NULL, legacy_contract_id INTEGER, cancel_date TEXT,
    cancel_reason TEXT, cancel_method TEXT, odometer_at_cancel INTEGER, refund_amount REAL, cancel_fee REAL, {STD});
"""
INDEXES = [f"CREATE INDEX ix_{t}_updated_at ON {t} (updated_at)" for t in TABLES] + [
    "CREATE INDEX ix_contracts_id ON contracts (legacy_contract_id)",
    "CREATE INDEX ix_claims_no ON claims (claim_no)",
]
FIRST_ID = 500_001
TEST_ID_START = 99_990_000


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    for stmt in INDEXES:
        conn.execute(stmt)


def dealers(core_dealers: pd.DataFrame, agents: pd.DataFrame, created: str) -> pd.DataFrame:
    """Legacy dealer rows for the legacy tenant's dealers onboarded before the cutover."""
    code = agents.set_index("agent_id")["agent_code"]
    d = core_dealers.copy()
    return pd.DataFrame({
        "dealer_code": d["dealer_number"], "dealer_name": d["dealer_name"], "city": d["city"], "state": d["state"],
        "agent_id": d["agent_id"].map(code), "reinsurance_company": d["dealer_name"].map(name_stem) + " Re Ltd",
        "status": "active", "onboarded_date": d["onboarded_date"], "created_at": created, "updated_at": created,
    })


def agents(core_agents: pd.DataFrame, created: str) -> pd.DataFrame:
    a = core_agents
    return pd.DataFrame({"agent_id": a["agent_code"], "agent_name": a["agent_name"], "territory": a["territory"],
                         "email": a["email"], "created_at": created, "updated_at": created})


def products(ref_rate: pd.DataFrame, ref_plan: pd.DataFrame, comp: pd.DataFrame, created: str) -> pd.DataFrame:
    """Stale product table: one list rate per (product, plan year, term), only up to the last legacy year."""
    p = ref_plan[ref_plan["subcategory_code"].isin(cat.LEGACY_SUBCATS)
                 & (ref_plan["plan_year"] <= cat.LEGACY_PRODUCTS_LAST_YEAR)]
    p = p.sort_values("deductible_amount", ascending=False).drop_duplicates(["product_code", "plan_year"])
    r = ref_rate.merge(p[["plan_id", "product_code", "plan_year", "deductible_amount"]], on="plan_id")
    r = r[r["odometer_band"].isin(["30-60K", "ALL"]) & r["class_code"].isin(["B", "ALL"])]
    r = r.merge(comp[["rate_id", "ADMIN_FEE", "CLAIMS_RESERVE", "COMMISSION"]], on="rate_id")
    tier = r["product_code"].map(lambda c: cat.SUBCAT_BY_CODE[c].name)
    return pd.DataFrame({
        "product_code": r["product_code"], "plan_year": r["plan_year"], "coverage_tier": tier,
        "term_months": r["term_months"], "term_miles": r["term_miles"], "deductible": r["deductible_amount"],
        "admin_fee": r["ADMIN_FEE"], "reserve_amount": r["CLAIMS_RESERVE"], "commission": r["COMMISSION"],
        "suggested_retail": r["suggested_retail"], "created_at": created, "updated_at": created,
    }).sort_values(["product_code", "plan_year", "term_months"]).reset_index(drop=True)


def contracts(deals: pd.DataFrame, rated: pd.DataFrame, comp: pd.DataFrame, agent_code: dict,
              rng: np.random.Generator) -> pd.DataFrame:
    """Project deals onto the legacy contract layout (status filled in later)."""
    n = len(deals)
    ids = np.arange(FIRST_ID, FIRST_ID + n)
    test = np.sort(rng.choice(n, max(3, int(n * 0.0008)), replace=False))  # test-id range by design
    ids[test] = TEST_ID_START + np.arange(1, len(test) + 1)
    c = comp.set_index("rate_id")
    rate = rated["_rate_id"].astype(int).to_numpy()
    servicing = rated["_d_agent_id"].map(agent_code).to_numpy().astype(object)
    writing = servicing.copy()
    moved = sim.pick(n, 0.10, rng)
    codes = sorted(set(agent_code.values()))
    writing[moved] = [codes[(codes.index(s) + 1) % len(codes)] for s in servicing[moved]]
    sale = deals["_sale_day"].to_numpy()
    created = sim.stamp(sale, rng)
    return pd.DataFrame({
        "legacy_contract_id": ids, "contract_no": [f"{p.split('-')[0]}{i:08d}" for p, i in
                                                    zip(deals["contract_number"], ids, strict=True)],
        "dealer_code": deals["dealer_number"].to_numpy(), "producer": servicing, "producer_code": writing,
        "product_code": deals["product_code"].to_numpy(), "plan_year": deals["_plan_year"].to_numpy(),
        "term_months": deals["term_months"].to_numpy(), "term_miles": deals["term_miles"].to_numpy(),
        "deductible": deals["_ded"].to_numpy(), "sale_date": deals["sale_date"].to_numpy(),
        "effective_date": deals["sale_date"].to_numpy(),
        "expiration_date": sim.iso(sim.add_months(sale, deals["term_months"].to_numpy())),
        **{k: deals[k].to_numpy() for k in ["customer_first_name", "customer_last_name", "customer_email",
                                            "customer_phone", "customer_zip", "customer_state", "vin",
                                            "vehicle_make", "vehicle_model", "vehicle_year", "new_used"]},
        "odometer_at_sale": deals["odometer"].to_numpy(), "retail_price": deals["purchase_price"].to_numpy(),
        "dealer_cost": deals["coverage_cost"].to_numpy(), "admin_fee": c.loc[rate, "ADMIN_FEE"].to_numpy(),
        "reserve_amount": (c.loc[rate, "CLAIMS_RESERVE"].to_numpy() + rated["_sur_amt"].to_numpy()
                           + rated["_opt_amt"].to_numpy()),
        "commission": c.loc[rate, "COMMISSION"].to_numpy(), "status": "active", "created_at": created,
        "updated_at": created,
    })


def claim_rows(pl: pd.DataFrame, cutover: np.datetime64, rng: np.random.Generator) -> pd.DataFrame:
    """Legacy claim rows for claims reported before the cutover (status frozen at the cutover)."""
    last = cutover - sim.DAY
    closed = (pl["close_day"].to_numpy() <= last) & ~pl["denied"].to_numpy(bool)
    denied = pl["denied"].to_numpy(bool) & (pl["decision_day"].to_numpy() <= last)
    status = np.where(closed, "paid", np.where(denied, "denied", "open"))
    paid = np.where(closed, -pl["paid_total"].to_numpy(), np.where(denied, 0.0, np.nan))
    upd_day = np.where(closed, pl["close_day"], np.where(denied, pl["decision_day"], pl["reported_day"]))
    created = sim.stamp(pl["reported_day"].to_numpy(), rng)
    updated = sim.stamp(upd_day.astype("M8[D]"), rng)
    comp = [cat.SUBCAT_BY_CODE[s].components[0].lower() for s in pl["subcat"]]
    return pd.DataFrame({
        "claim_no": [f"{int(x):07d}" for x in pl["claim_number"]],
        "legacy_contract_id": pl["legacy_contract_ref"].to_numpy(),
        "loss_date": sim.iso(pl["loss_day"].to_numpy()), "reported_date": sim.iso(pl["reported_day"].to_numpy()),
        "paid_date": np.where(closed, sim.iso(pl["close_day"].to_numpy()), None),
        "odometer_at_loss": pl["_odometer"].to_numpy(), "repair_category": comp,
        "amount_requested": -(pl["req"].to_numpy() + pl["ded"].to_numpy()), "amount_paid": paid, "status": status,
        "created_at": created, "updated_at": np.where(updated > created, updated, created),
    })


def cancel_rows(cx: pd.DataFrame, first_seq: int) -> pd.DataFrame:
    return pd.DataFrame({
        "cancellation_id": [f"CXL{first_seq + i:08d}" for i in range(len(cx))],
        "legacy_contract_id": cx["legacy_contract_id"].to_numpy(), "cancel_date": cx["cancel_date"].to_numpy(),
        "cancel_reason": cx["reason"].to_numpy(), "cancel_method": cx["method"].to_numpy(),
        "odometer_at_cancel": cx["odometer"].to_numpy(), "refund_amount": cx["customer_refund"].to_numpy(),
        "cancel_fee": cx["cancel_fee"].to_numpy(), "created_at": cx["processed_at"].to_numpy(),
        "updated_at": cx["processed_at"].to_numpy(),
    })


def _duplicate(df: pd.DataFrame, rate: float, rng: np.random.Generator) -> pd.DataFrame:
    return pd.concat([df, df.iloc[sim.pick(len(df), rate, rng)]], ignore_index=True)


def mess_contracts(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    n = len(df)
    i = sim.pick(n, 0.02, rng)
    df.loc[df.index[i], "dealer_code"] = df["dealer_code"].iloc[i].str.lower()
    i = sim.pick(n, 0.005, rng)
    df.loc[df.index[i], "dealer_code"] = " " + df["dealer_code"].iloc[i] + " "
    df.loc[df.index[sim.pick(n, 0.004, rng)], "dealer_code"] = None
    for idx in sim.pick(n, 0.012, rng):  # bad VINs: short, illegal letter, lowercase, missing
        vin = df.at[df.index[idx], "vin"]
        df.at[df.index[idx], "vin"] = [vin[:16], vin[:8] + "O" + vin[9:], vin.lower(), None][idx % 4]
    i = sim.pick(n, 0.0005, rng)  # old export format the data contract rejects
    df.loc[df.index[i], "sale_date"] = pd.to_datetime(df["sale_date"].iloc[i]).dt.strftime("%m/%d/%Y")
    return _duplicate(df, 0.004, rng)


def mess_claims(df: pd.DataFrame, sale_day: np.ndarray, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    i = sim.pick(len(df), 0.003, rng)
    df.loc[df.index[i], "loss_date"] = sim.iso(sale_day[i] - rng.integers(5, 200, len(i)) * sim.DAY)
    return _duplicate(df, 0.003, rng)


def mess_cancellations(df: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    df = df.copy()
    i = sim.pick(len(df), 0.002, rng)
    df.loc[df.index[i], "refund_amount"] = -df["refund_amount"].iloc[i]
    return _duplicate(df, 0.003, rng)
