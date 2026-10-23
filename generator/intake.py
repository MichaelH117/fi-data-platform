"""Two more intake sources next to the dealer-integration feed: a CRM / call-center export and an
admin-API page drop.

``<intake>/crm/YYYY-MM-DD/calls.csv`` + ``_manifest.json``
    The call-center CRM's daily export of customer contacts (calls, chats, e-mails): complaints,
    cancel requests, claim-status questions, coverage and billing questions. The CRM is a separate
    system: it knows the customer by a hashed phone number and, when the agent typed it in, by the
    contract number. It went live on ``CRM_START``; the first export is a FULL history file and every
    appended day is a DELTA. Deliberate defects: no customer key at all, an unparseable timestamp,
    a malformed contract number and a duplicated call_id.
``<intake>/admin_api/YYYY-MM-DD/dealer/page_NNNN.json``
    A paginated REST extract of the dealer master (``GET /dealers?page=N``), one JSON page per file
    with ``page``, ``page_count``, ``total_records`` and ``records``. Deliberate defects: on some days
    the last page never arrives (the extract is incomplete and must be rejected whole) and one record
    carries a full state name instead of the two-letter code.

``<intake>`` defaults to a sibling of the landing folder (``data/intake``; env ``INTAKE_DIR``).
Everything is derived from Admin Core rows already written, so the sources agree with each other.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from generator import sim
from generator.landing import sha256

CRM_START = date(2025, 1, 1)
CALLS_FILE = "calls.csv"
MANIFEST = "_manifest.json"
CALL_COLS = ["call_id", "call_at", "channel", "queue", "reason", "disposition", "tenant_code", "contract_number",
             "phone_hash", "complaint_category", "is_regulator_referral", "handle_seconds"]
API_ENTITY = "dealer"
API_PAGE_SIZE = 40
API_COLS = ["dealer_number", "dealer_name", "tenant_code", "state", "status", "remit_rule", "updated_at"]


def phone_hash(phone) -> str | None:
    """sha256 of the digits of a phone number: the CRM's customer key (same rule for any consumer)."""
    if phone is None or (isinstance(phone, float) and np.isnan(phone)):
        return None
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    return hashlib.sha256(digits.encode()).hexdigest() if digits else None


def default_dir(landing: Path) -> Path:
    return landing.parent / "intake"


# --------------------------------------------------------------------------- CRM export
def _contracts(conn: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql("""select c.contract_id, c.contract_number, c.customer_phone, a.administrator_code tenant_code,
                                 c.entered_date, c.status
                          from contract c join administrator a on a.tenant_id = c.tenant_id
                          where c.removed_from_source = 0""", conn).set_index("contract_id")


def crm_calls(conn: sqlite3.Connection, lo: np.datetime64 | None, hi: np.datetime64,
              rng: np.random.Generator) -> pd.DataFrame:
    """Customer contacts with a call day in (lo, hi], derived from Admin Core events."""
    start = np.datetime64(CRM_START)
    lo = start - sim.DAY if lo is None or lo < start - sim.DAY else lo
    con = _contracts(conn)
    parts = []

    def window(days: np.ndarray) -> np.ndarray:
        return (days > lo) & (days <= hi)

    comp = pd.read_sql("select contract_id, received_date, category, is_regulator from complaint "
                       "where contract_id is not null", conn)
    d = sim.parse_days(comp["received_date"])
    comp = comp[window(d)]
    parts.append(pd.DataFrame({"contract_id": comp["contract_id"].to_numpy(), "day": d[window(d)],
                               "reason": "COMPLAINT", "queue": "COMPLIANCE",
                               "complaint_category": comp["category"].to_numpy(),
                               "is_regulator_referral": comp["is_regulator"].to_numpy()}))
    cx = pd.read_sql("select contract_id, cancel_date from cancellation where initiated_by = 'CUSTOMER'", conn)
    cx = cx[rng.random(len(cx)) < 0.7]
    d = sim.parse_days(cx["cancel_date"]) - rng.integers(0, 4, len(cx)) * sim.DAY
    parts.append(pd.DataFrame({"contract_id": cx["contract_id"].to_numpy()[window(d)], "day": d[window(d)],
                               "reason": "CANCEL_REQUEST", "queue": "CANCELLATIONS"}))
    cl = pd.read_sql("select contract_id, reported_at from claim where contract_id is not null", conn)
    cl = cl[rng.random(len(cl)) < 0.25]
    d = sim.parse_days(cl["reported_at"]) + rng.integers(2, 21, len(cl)) * sim.DAY
    parts.append(pd.DataFrame({"contract_id": cl["contract_id"].to_numpy()[window(d)], "day": d[window(d)],
                               "reason": "CLAIM_STATUS", "queue": "CLAIMS"}))
    live = con[con["status"].isin(["INFORCE", "PENDING"])]
    n_days = int((hi - lo) / sim.DAY)
    k = int(rng.poisson(max(len(live), 1) * 0.00025 * n_days))
    if k and len(live):
        pick = live.index.to_numpy()[rng.integers(0, len(live), k)]
        d = lo + rng.integers(1, n_days + 1, k) * sim.DAY
        parts.append(pd.DataFrame({"contract_id": pick, "day": d,
                                   "reason": sim.weighted_choice(rng, ["COVERAGE_QUESTION", "BILLING"], [0.7, 0.3], k),
                                   "queue": "CUSTOMER_CARE"}))
    calls = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else None
    if calls is None:
        return pd.DataFrame(columns=CALL_COLS)
    calls = calls[calls["contract_id"].isin(con.index)].sort_values(["day", "contract_id", "reason"], kind="stable")
    calls = calls.reset_index(drop=True)
    n = len(calls)
    c = con.loc[calls["contract_id"]]
    has_number = rng.random(n) < 0.85
    has_phone = ~has_number | (rng.random(n) < 0.6)
    out = pd.DataFrame({
        "call_id": [f"CC{day.replace('-', '')}{i:05d}" for i, day in enumerate(sim.iso(calls["day"].to_numpy()))],
        "call_at": sim.stamp(calls["day"].to_numpy(), rng),
        "channel": sim.weighted_choice(rng, ["PHONE", "CHAT", "EMAIL"], [0.7, 0.2, 0.1], n),
        "queue": calls["queue"].to_numpy(), "reason": calls["reason"].to_numpy(),
        "disposition": np.where(calls["reason"].to_numpy() == "COMPLAINT", "ESCALATED", sim.weighted_choice(
            rng, ["RESOLVED", "CALLBACK", "TRANSFERRED"], [0.75, 0.15, 0.10], n)),
        "tenant_code": c["tenant_code"].to_numpy(),
        "contract_number": np.where(has_number, c["contract_number"].to_numpy(), None),
        "phone_hash": np.where(has_phone, [phone_hash(p) for p in c["customer_phone"]], None),
        "complaint_category": calls.get("complaint_category", pd.Series(None, index=calls.index)).to_numpy(),
        "is_regulator_referral": calls.get("is_regulator_referral", pd.Series(0, index=calls.index)).fillna(0)
        .astype(int).to_numpy(),
        "handle_seconds": rng.integers(60, 1500, n),
    })
    return out[CALL_COLS]


def inject_crm_defects(calls: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """No customer key, bad timestamp, malformed contract number, duplicated call_id."""
    calls = calls.copy()
    n = len(calls)
    if n < 4:
        return calls
    i = rng.choice(n, max(1, n // 150) * 3, replace=False)
    a, b, c = np.array_split(i, 3)
    calls.loc[a, ["contract_number", "phone_hash"]] = None
    calls.loc[b, "call_at"] = calls.loc[b, "call_at"].str.slice(0, 10).str.replace("-", "/") + " 25:61"
    calls.loc[c, "contract_number"] = calls.loc[c, "tenant_code"] + "#" + calls.loc[c, "call_id"].str[-4:]
    dup = calls.iloc[rng.choice(n, max(1, n // 200), replace=False)]
    return pd.concat([calls, dup], ignore_index=True)


def write_crm(conn: sqlite3.Connection, intake: Path, lo: np.datetime64 | None, hi: np.datetime64, seq_no: int,
              rng: np.random.Generator) -> Path | None:
    """Write the CRM export for (lo, hi] as ``crm/<hi>/``; FULL when ``lo`` is None, else DELTA."""
    if hi < np.datetime64(CRM_START):
        return None
    calls = inject_crm_defects(crm_calls(conn, lo, hi, rng), rng)
    folder = intake / "crm" / str(hi)
    folder.mkdir(parents=True, exist_ok=True)
    calls.to_csv(folder / CALLS_FILE, index=False, lineterminator="\n")
    manifest = {"export_date": str(hi), "sequence_no": seq_no, "export_type": "FULL" if lo is None else "DELTA",
                "window_start": str(max(lo + sim.DAY, np.datetime64(CRM_START))) if lo is not None else str(CRM_START),
                "window_end": str(hi), "generated_at": f"{hi} 23:30:00",
                "files": {CALLS_FILE: {"row_count": len(calls), "sha256": sha256(folder / CALLS_FILE)}}}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- admin API page drop
def api_pages_missing(seq_no: int) -> bool:
    """Deterministic schedule: every fourth extract loses its last page in transit."""
    return seq_no % 4 == 0


def write_admin_api(conn: sqlite3.Connection, intake: Path, day: np.datetime64, seq_no: int) -> Path:
    """Paginated dealer-master extract as of ``day`` (one JSON page per file)."""
    d = pd.read_sql("""select d.dealer_number, d.dealer_name, a.administrator_code tenant_code, d.state, d.status,
                              d.remit_rule, d.updated_at
                       from dealer d join administrator a on a.tenant_id = d.tenant_id
                       where d.removed_from_source = 0 order by d.dealer_number""", conn)
    recs = d.astype(object).where(d.notna(), None).to_dict("records")
    if recs:  # a record keyed in by hand on the admin side: full state name instead of the code
        k = seq_no % len(recs)
        recs[k] = {**recs[k], "state": {"TX": "Texas", "FL": "Florida"}.get(recs[k]["state"], "Unknown")}
    pages = [recs[i:i + API_PAGE_SIZE] for i in range(0, len(recs), API_PAGE_SIZE)] or [[]]
    folder = intake / "admin_api" / str(day) / API_ENTITY
    folder.mkdir(parents=True, exist_ok=True)
    for p, rows in enumerate(pages, start=1):
        if api_pages_missing(seq_no) and p == len(pages) and len(pages) > 1:
            continue
        body = {"entity": API_ENTITY, "request": f"GET /api/v1/{API_ENTITY}s?page={p}&page_size={API_PAGE_SIZE}",
                "extract_date": str(day), "sequence_no": seq_no, "extracted_at": f"{day} 23:45:00", "page": p,
                "page_count": len(pages), "page_size": API_PAGE_SIZE, "total_records": len(recs), "records": rows}
        (folder / f"page_{p:04d}.json").write_text(json.dumps(body, indent=1, sort_keys=True) + "\n",
                                                   encoding="utf-8")
    return folder


def write_all(core_path: Path, intake: Path, lo: np.datetime64 | None, hi: np.datetime64, seq_no: int,
              seed: int) -> dict[str, int]:
    """Both drops for one day. Deterministic for (seed, seq_no)."""
    with sqlite3.connect(core_path) as conn:
        crm = write_crm(conn, intake, lo, hi, seq_no, np.random.default_rng([seed, 11, seq_no]))
        write_admin_api(conn, intake, hi, seq_no)
    rows = 0 if crm is None else json.loads((crm / MANIFEST).read_text())["files"][CALLS_FILE]["row_count"]
    return {"crm.calls": rows, "admin_api.pages": len(list((intake / "admin_api" / str(hi) / API_ENTITY)
                                                           .glob("page_*.json")))}
