"""Admin Core custom fields: the entity-attribute-value (EAV) store for specialty-line attributes.

Admin systems rarely add a column per product attribute. RV contracts carry their coach and chassis
details as *custom fields*: one ``custom_field_def`` row per field, and one ``contract_custom_field``
row per (contract, field) with the value stored as text whatever its real type. Analysts pivot it back
with ``max(case when field_id = ... then value_text end)``; staging does that once (docs/V3_DESIGN.md 8.3).

Everything here is a pure function of the contract rows (no random draws), so adding the EAV store
does not shift any other generated value and the legacy build can rebuild the same rows from raw.
"""

from __future__ import annotations

import zlib

import pandas as pd

from generator import catalog as cat

# field_id, field_name, data_type (the type the text value really holds)
FIELD_DEFS: list[tuple[int, str, str]] = [
    (101, "Coach Make", "text"),
    (102, "Coach Model", "text"),
    (103, "Chassis Make", "text"),
    (104, "Chassis VIN", "text"),
    (105, "Model Year", "integer"),
]
FIELD_ID = {name: fid for fid, name, _ in FIELD_DEFS}
RV_CLASS = "RV"


def field_defs(tenant_ids, created: str) -> pd.DataFrame:
    """One definition row per tenant and field (field ids are global, rows are tenant-scoped)."""
    rows = [{"field_def_id": i * 100 + fid, "tenant_id": int(t), "field_id": fid, "field_name": name,
             "data_type": dtype, "created_at": created, "updated_at": created}
            for i, t in enumerate(sorted({int(x) for x in tenant_ids}), start=1)
            for fid, name, dtype in FIELD_DEFS]
    return pd.DataFrame(rows)


def _chassis_make(contract_number: str) -> str:
    makes = cat.RV_CHASSIS_MAKES
    return makes[zlib.crc32(str(contract_number).encode()) % len(makes)]


def contract_values(contracts: pd.DataFrame, start_id: int = 1) -> pd.DataFrame:
    """EAV rows for every RV contract. A blank chassis VIN has no row (missing, not empty)."""
    rv = contracts[contracts["class_code"] == RV_CLASS]
    cols = ["custom_field_id", "tenant_id", "contract_id", "field_id", "value_text", "created_at", "updated_at"]
    if rv.empty:
        return pd.DataFrame(columns=cols)
    parts = []
    for name, values in (("Coach Make", rv["vehicle_make"]), ("Coach Model", rv["vehicle_model"]),
                         ("Chassis Make", rv["contract_number"].map(_chassis_make)),
                         ("Chassis VIN", rv["chassis_vin"]), ("Model Year", rv["vehicle_year"])):
        part = pd.DataFrame({"tenant_id": rv["tenant_id"].to_numpy(), "contract_id": rv["contract_id"].to_numpy(),
                             "field_id": FIELD_ID[name], "value": values.to_numpy(),
                             "created_at": rv["created_at"].to_numpy(), "updated_at": rv["created_at"].to_numpy()})
        parts.append(part[part["value"].notna()])
    out = pd.concat(parts, ignore_index=True).sort_values(["contract_id", "field_id"], kind="stable")
    out["value_text"] = [str(int(v)) if isinstance(v, float) and v.is_integer() else str(v) for v in out["value"]]
    out["custom_field_id"] = range(start_id, start_id + len(out))
    return out[cols].reset_index(drop=True)
