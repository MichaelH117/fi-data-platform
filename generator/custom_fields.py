"""Admin Core custom fields: the entity-attribute-value (EAV) store for specialty-line attributes.

Admin systems rarely add a column per product attribute. RV contracts carry their coach and chassis
details as *custom fields*: one ``custom_field_def`` row per field, and one ``contract_custom_field``
row per (contract, field) with the value stored as text whatever its real type. Analysts pivot it back
with ``max(case when field_id = ... then value_text end)``; staging does that once (docs/V3_DESIGN.md 8.3).

Everything here is a pure function of the contract rows (no random draws), so adding the EAV store
does not shift any other generated value and the legacy build can rebuild the same rows from raw.
V7 adds two things a real admin user produces: chassis VINs typed badly (edit 27) and a parent-contract
link on add-on contracts, as free text that is sometimes junk (edit 26).
"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from generator import catalog as cat

# field_id, field_name, data_type (the type the text value really holds)
FIELD_DEFS: list[tuple[int, str, str]] = [
    (101, "Coach Make", "text"),
    (102, "Coach Model", "text"),
    (103, "Chassis Make", "text"),
    (104, "Chassis VIN", "text"),
    (105, "Model Year", "integer"),
    (106, "Parent Contract Number", "text"),  # V7 edit 26: an add-on points to the VSC it was sold with
]
FIELD_ID = {name: fid for fid, name, _ in FIELD_DEFS}
RV_CLASS = "RV"
# V7 edit 27: what the admin user typed into the Chassis VIN custom field (the contract column stays clean).
# Shares are percent of RV contracts that have a chassis VIN, picked by a stable hash of the contract number.
CHASSIS_VIN_CASES = (("placeholder", 3), ("bad_length", 3), ("lowercase", 4), ("duplicate_across_contracts", 2))
CHASSIS_PLACEHOLDERS = ("UNKNOWN", "00000000000000000", "TBD")
# V7 edit 26: share (percent) of add-on contracts that carry a parent link, and of links typed badly.
PARENT_LINK_PCT, PARENT_JUNK_PCT = 40, 12
PARENT_JUNK_KINDS = ("lowercase", "digits_only", "padded", "not_applicable", "free_text", "no_such_contract")
PREFIX_CATEGORY = {(s.prefix or s.code): s.category for s in cat.SUBCATEGORIES}
ADD_ON_CATEGORIES = {"ANCILLARY"}


def field_defs(tenant_ids, created: str) -> pd.DataFrame:
    """One definition row per tenant and field (field ids are global, rows are tenant-scoped)."""
    rows = [{"field_def_id": i * 100 + fid, "tenant_id": int(t), "field_id": fid, "field_name": name,
             "data_type": dtype, "created_at": created, "updated_at": created}
            for i, t in enumerate(sorted({int(x) for x in tenant_ids}), start=1)
            for fid, name, dtype in FIELD_DEFS]
    return pd.DataFrame(rows)


def _h(tag: str, values) -> np.ndarray:
    return np.array([zlib.crc32(f"{tag}|{v}".encode()) for v in values], dtype=np.int64)


def _chassis_make(contract_number: str) -> str:
    makes = cat.RV_CHASSIS_MAKES
    return makes[zlib.crc32(str(contract_number).encode()) % len(makes)]


def chassis_vin_case(rv: pd.DataFrame) -> pd.Series:
    """The chassis-VIN data-quality case each RV contract's custom field is generated as (V7 edit 27):
    valid / missing (no row) / placeholder / bad_length / lowercase / duplicate_across_contracts."""
    h = _h("chassis", rv["contract_number"]) % 100
    case = np.where(rv["chassis_vin"].isna(), "missing", "valid").astype(object)
    lo = 0
    for name, pct in CHASSIS_VIN_CASES:
        case = np.where((case == "valid") & (h >= lo) & (h < lo + pct), name, case)
        lo += pct
    return pd.Series(case, index=rv.index)


def chassis_vin_values(rv: pd.DataFrame) -> pd.Series:
    """The text typed into the Chassis VIN field, per RV contract (None = no row). ``rv`` sorted by id."""
    case = chassis_vin_case(rv)
    vin = rv["chassis_vin"].astype(object)
    h = pd.Series(_h("chassis-v", rv["contract_number"]), index=rv.index)
    prev = vin.ffill().shift(1)  # the chassis VIN of an earlier RV contract
    out = vin.where(case == "valid", None).astype(object)
    ph = case == "placeholder"
    out[ph] = [CHASSIS_PLACEHOLDERS[k % len(CHASSIS_PLACEHOLDERS)] for k in h[ph]]
    bl = case == "bad_length"
    out[bl] = [v[:16] if k % 2 else v[:11] for v, k in zip(vin[bl], h[bl], strict=True)]
    low = case == "lowercase"
    out[low] = vin[low].str.lower()
    dup = case == "duplicate_across_contracts"
    out[dup] = prev[dup].where(prev[dup].notna(), vin[dup])
    return out


def parent_links(contracts: pd.DataFrame) -> pd.DataFrame:
    """V7 edit 26: add-on contracts sold alongside a VSC point to it with a text custom field. The parent is
    the latest VSC contract entered at the same dealer on or before the add-on (a pure function of the
    rows). Some links are typed badly: case, a dropped prefix, padding, "N/A", free text, or a number
    that matches no contract. Returns contract_id, tenant_id, created_at, value, parent_number, kind."""
    cols = ["contract_id", "tenant_id", "created_at", "value", "parent_number", "kind"]
    c = contracts[["contract_id", "tenant_id", "dealer_id", "contract_number", "entered_date", "created_at"]].copy()
    cat_of = c["contract_number"].astype(str).str.split("-").str[0].map(PREFIX_CATEGORY)
    c["_day"] = pd.to_datetime(c["entered_date"])
    vsc = c[(cat_of == "VSC").to_numpy()].sort_values("_day", kind="stable")
    pick = cat_of.isin(ADD_ON_CATEGORIES).to_numpy() & (_h("parent", c["contract_number"]) % 100 < PARENT_LINK_PCT)
    kids = c[pick]
    if kids.empty or vsc.empty:
        return pd.DataFrame(columns=cols)
    m = pd.merge_asof(kids.sort_values("_day", kind="stable"),
                      vsc[["_day", "dealer_id", "tenant_id", "contract_number"]].rename(
                          columns={"contract_number": "parent_number"}),
                      on="_day", by=["dealer_id", "tenant_id"], direction="backward")
    m = m[m["parent_number"].notna()].sort_values("contract_id", kind="stable").reset_index(drop=True)
    h = _h("parent-junk", m["contract_number"])
    junk = h % 100 < PARENT_JUNK_PCT
    kind = np.where(junk, np.array(PARENT_JUNK_KINDS, dtype=object)[(h // 100) % len(PARENT_JUNK_KINDS)], "valid")
    p = m["parent_number"].astype(str)
    digits = p.str.split("-").str[1].str.lstrip("0")
    value = np.select(
        [kind == "lowercase", kind == "digits_only", kind == "padded", kind == "not_applicable",
         kind == "free_text", kind == "no_such_contract"],
        [p.str.lower(), digits, " " + p + "  ", "N/A", "sold w/ vsc - see deal", p + "7"], p)
    m["value"], m["kind"] = value, kind
    return m[cols]


def contract_values(contracts: pd.DataFrame, start_id: int = 1) -> pd.DataFrame:
    """EAV rows: coach/chassis fields for every RV contract (a blank chassis VIN has no row: missing, not
    empty) plus the parent-contract link on add-on contracts sold with a VSC."""
    cols = ["custom_field_id", "tenant_id", "contract_id", "field_id", "value_text", "created_at", "updated_at"]
    rv = contracts[contracts["class_code"] == RV_CLASS].sort_values("contract_id", kind="stable")
    parts = []
    if not rv.empty:
        for name, values in (("Coach Make", rv["vehicle_make"]), ("Coach Model", rv["vehicle_model"]),
                             ("Chassis Make", rv["contract_number"].map(_chassis_make)),
                             ("Chassis VIN", chassis_vin_values(rv)), ("Model Year", rv["vehicle_year"])):
            part = pd.DataFrame({"tenant_id": rv["tenant_id"].to_numpy(), "contract_id": rv["contract_id"].to_numpy(),
                                 "field_id": FIELD_ID[name], "value": values.to_numpy(),
                                 "created_at": rv["created_at"].to_numpy(), "updated_at": rv["created_at"].to_numpy()})
            parts.append(part[part["value"].notna()])
    if {"dealer_id", "entered_date"} <= set(contracts.columns):
        pl = parent_links(contracts)
        if len(pl):
            parts.append(pd.DataFrame({
                "tenant_id": pl["tenant_id"].to_numpy(), "contract_id": pl["contract_id"].to_numpy(),
                "field_id": FIELD_ID["Parent Contract Number"], "value": pl["value"].to_numpy(),
                "created_at": pl["created_at"].to_numpy(), "updated_at": pl["created_at"].to_numpy()}))
    if not parts:
        return pd.DataFrame(columns=cols)
    out = pd.concat(parts, ignore_index=True).sort_values(["contract_id", "field_id"], kind="stable")
    out["value_text"] = [str(int(v)) if isinstance(v, float) and v.is_integer() else str(v) for v in out["value"]]
    out["custom_field_id"] = range(start_id, start_id + len(out))
    return out[cols].reset_index(drop=True)
