"""Dealer-integration network landing feed: one folder of drop files per business day.

``data/landing/YYYY-MM-DD/`` holds ``deals.csv``, ``remittance.csv``, ``econtract_events.jsonl``
and ``_manifest.json`` (row count + sha256 per file, feed date, sequence number). The files carry
deliberate defects so the intake validation has something to catch:

file level (the whole file is rejected; the network re-sends it the next day)
  - wrong checksum in the manifest
  - row-count mismatch in the manifest
row level (only the bad rows are rejected)
  - duplicate resend of rows already delivered, bad VIN, unknown dealer,
    remittance without a deal
business events that are valid but need handling downstream
  - deal never remitted, late remittance, void after contracting
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from generator.deals import DEAL_COLS

REMIT_COLS = ["remit_batch_id", "dealer_number", "contract_number", "remit_amount", "remit_date", "entered_date"]
EVENT_COLS = ["event_id", "contract_number", "event_type", "event_at", "payload"]
FILES = {"deals": "deals.csv", "remittance": "remittance.csv", "econtract_events": "econtract_events.jsonl"}
MANIFEST = "_manifest.json"


def file_defects(seq_no: int) -> dict[str, str]:
    """Deterministic file-level defect schedule by drop sequence number."""
    out = {}
    if seq_no % 7 == 1:
        out["econtract_events"] = "checksum"
    if seq_no % 7 == 2:
        out["remittance"] = "row_count"
    return out


def wants_duplicates(seq_no: int) -> bool:
    return seq_no % 3 == 0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inject_row_defects(deals: pd.DataFrame, remits: pd.DataFrame, rng: np.random.Generator, seq_no: int,
                       prev_deals: pd.DataFrame | None, day: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Bad VINs, unknown dealers, remittance without a deal and duplicate resends."""
    deals, remits = deals.copy(), remits.copy()
    stats = {"bad_vin": 0, "unknown_dealer": 0, "remit_without_deal": 0, "duplicate_resend": 0}
    if len(deals):
        bad = np.flatnonzero(rng.random(len(deals)) < 0.006)
        if len(bad) == 0 and len(deals) >= 5:  # at least one bad VIN in a normal-sized drop
            bad = np.array([int(rng.integers(len(deals)))])
        for k, i in enumerate(bad):
            vin = str(deals.iat[i, deals.columns.get_loc("vin")])
            deals.iat[i, deals.columns.get_loc("vin")] = [vin[:15], vin[:8] + "I" + vin[9:], vin.lower()][k % 3]
        stats["bad_vin"] = len(bad)
        if len(deals) >= 3 and rng.random() < 0.7:  # a deal from a dealer the admin never set up
            unk = int(rng.integers(len(deals)))
            deals.iat[unk, deals.columns.get_loc("dealer_number")] = f"D{int(rng.integers(7000, 7999))}"
            stats["unknown_dealer"] = 1
    ghosts = int(rng.integers(1, 3))
    ghost = pd.DataFrame({
        "remit_batch_id": [f"RB-GHOST-{seq_no}-{k}" for k in range(ghosts)],
        "dealer_number": remits["dealer_number"].iloc[:ghosts].tolist() + ["D1001"] * max(0, ghosts - len(remits)),
        "contract_number": [f"VSC-{int(x):08d}" for x in rng.integers(80_000_000, 89_999_999, ghosts)],
        "remit_amount": np.round(rng.uniform(400, 2500, ghosts), 2),
        "remit_date": str(np.datetime64(day) - np.timedelta64(1, "D")),
        "entered_date": str(np.datetime64(day) - np.timedelta64(2, "D")),
    })
    remits = pd.concat([remits, ghost], ignore_index=True)
    stats["remit_without_deal"] = ghosts
    if wants_duplicates(seq_no) and prev_deals is not None and len(prev_deals):
        dup = prev_deals.sample(min(len(prev_deals), int(rng.integers(3, 9))), random_state=int(rng.integers(1 << 31)))
        deals = pd.concat([deals, dup[deals.columns]], ignore_index=True)
        stats["duplicate_resend"] = len(dup)
    return deals, remits, stats


def write_drop(root: Path, day: str, seq_no: int, deals: pd.DataFrame, remits: pd.DataFrame,
               events: list[dict], generated_at: str) -> Path:
    """Write one day's folder. Applies the file-level defect schedule to the manifest only."""
    folder = root / day
    folder.mkdir(parents=True, exist_ok=True)
    out = deals[DEAL_COLS].copy()
    for col in ("term_miles", "msrp", "finance_amount"):  # whole numbers print without ".0"
        num = pd.to_numeric(out[col], errors="coerce")
        whole = (num.notna() & (num % 1 == 0)).to_numpy()
        vals = out[col].astype(object).to_numpy()
        vals[whole] = num[whole].astype("int64").astype(str).to_numpy()
        out[col] = vals
    out.to_csv(folder / FILES["deals"], index=False, lineterminator="\n")
    remits[REMIT_COLS].to_csv(folder / FILES["remittance"], index=False, lineterminator="\n")
    with (folder / FILES["econtract_events"]).open("w", encoding="utf-8", newline="\n") as f:
        for e in events:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    counts = {"deals": len(deals), "remittance": len(remits), "econtract_events": len(events)}
    defects = file_defects(seq_no)
    files = {}
    for key, name in FILES.items():
        digest, rows = sha256(folder / name), counts[key]
        if defects.get(key) == "checksum":
            digest = hashlib.sha256(f"tampered-{seq_no}".encode()).hexdigest()
        if defects.get(key) == "row_count":
            rows += 1
        files[name] = {"row_count": rows, "sha256": digest}
    manifest = {"feed_date": day, "sequence_no": seq_no, "generated_at": generated_at, "files": files}
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return folder


def read_deals(folder: Path) -> pd.DataFrame | None:
    path = folder / FILES["deals"]
    return pd.read_csv(path, dtype=str, keep_default_na=False) if path.exists() else None


def contract_events(deals: pd.DataFrame, voids: pd.DataFrame, seq_no: int) -> list[dict]:
    """RATED + CONTRACTED per deal, and VOIDED for contracts voided that day."""
    out = []
    k = 0
    for r in deals.itertuples(index=False):
        for kind, t in (("RATED", "09:00:00"), ("CONTRACTED", "09:30:00")):
            k += 1
            out.append({"event_id": f"EV{seq_no:05d}{k:06d}", "contract_number": r.contract_number,
                        "event_type": kind, "event_at": f"{r.sale_date} {t}",
                        "payload": {"deal_id": r.deal_id, "dealer_number": r.dealer_number,
                                    "plan_code": r.plan_code, "coverage_cost": float(r.coverage_cost)}})
    for r in voids.itertuples(index=False):
        k += 1
        out.append({"event_id": f"EV{seq_no:05d}{k:06d}", "contract_number": r.contract_number,
                    "event_type": "VOIDED", "event_at": f"{r.day} 15:00:00",
                    "payload": {"dealer_number": r.dealer_number, "reason": "DEAL_UNWOUND_BEFORE_FUNDING"}})
    return out
