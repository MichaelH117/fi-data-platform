"""Dealer-integration network intake: validate landing drops, quarantine bad rows, promote the rest.

CLI: ``python -m ingest.feed [--date YYYY-MM-DD] [--landing DIR] [--admin-core PATH]``

For each unprocessed folder ``data/landing/YYYY-MM-DD/`` (oldest first, or only ``--date``):
1. File checks against ``_manifest.json``: sha256, row count, and "this exact file was already
   loaded" (duplicate resend). A failing file is rejected whole.
2. Row checks against the data contracts ``contracts/feed_*.yml`` (required, type, pattern,
   strict accepted values).
3. Business rules against Admin Core (unknown dealer, no rate, duplicate deal, remittance
   without a deal, void after remittance, ...).
4. Promotion into Admin Core with the same booking rules the admin system uses: deals become
   PENDING contracts (+ SALE txn, disbursements, commission); VOIDED events void them; remittances
   create the batch, cash lines and CASH txns and move the contract to INFORCE.
Every file gets an ``intake_file`` row and every rejected row an ``intake_reject`` row (reason +
raw JSON). One folder is one SQLite transaction, so a crash never half-promotes a drop, and a
folder that already has ``intake_file`` rows is skipped, so re-runs are idempotent.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from generator import ADMIN_CORE_PATH, LANDING_DIR
from generator import admin_core as ac
from generator.booking import Ids, Ref, rate_deals, remit_rows, sale_rows
from generator.landing import EVENT_COLS, FILES, MANIFEST, sha256
from ingest import ROOT
from ingest.contract import load_contract, validate

log = logging.getLogger("ingest.feed")
CONTRACTS = {key: ROOT / "contracts" / f"feed_{key}.yml" for key in FILES}
NETWORK_TENANT = 0


@dataclass
class FileResult:
    feed_date: str
    file_name: str
    status: str  # LOADED | REJECTED | DUPLICATE_FILE | MISSING
    rows: int = 0
    promoted: int = 0
    rejected: int = 0
    message: str = ""
    reasons: dict[str, int] = field(default_factory=dict)


def read_rows(path: Path) -> pd.DataFrame:
    """Read a landing file as untouched text (CSV) or flattened JSON lines (payload kept as JSON text)."""
    if path.suffix == ".jsonl":
        recs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if not recs:
            return pd.DataFrame(columns=EVENT_COLS, dtype=object)
        df = pd.DataFrame(recs)
        if "payload" in df:
            df["payload"] = df["payload"].map(lambda p: json.dumps(p, sort_keys=True) if p is not None else None)
        return df.astype(object).where(df.notna(), None).astype("string").astype(object)
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False).replace("", None)
    except pd.errors.EmptyDataError:  # zero-byte file: no header, so the schema check rejects it
        return pd.DataFrame()


def pending_folders(conn: sqlite3.Connection, landing: Path, only: str | None = None) -> list[Path]:
    done = {r[0] for r in conn.execute("select distinct feed_date from intake_file")}
    folders = sorted(p for p in landing.iterdir() if p.is_dir() and (p / MANIFEST).exists()) if landing.exists() else []
    if only:
        folders = [p for p in folders if p.name == only]
    return [p for p in folders if p.name not in done]


class _Folder:
    """Mutable working state for one drop."""

    def __init__(self, conn: sqlite3.Connection, folder: Path) -> None:
        self.conn, self.folder, self.feed_date = conn, folder, folder.name
        self.processed_at = f"{self.feed_date} 06:15:00"
        self.ids = Ids(conn)
        self.rejects: list[dict] = []
        self.results: list[FileResult] = []

    def reject(self, file_id: int, name: str, df: pd.DataFrame, reasons: pd.Series, tenant=NETWORK_TENANT) -> None:
        bad = reasons[reasons != ""]
        if bad.empty:
            return
        raw = df.loc[bad.index].astype(object).where(df.loc[bad.index].notna(), None)
        ten = tenant.loc[bad.index] if isinstance(tenant, pd.Series) else pd.Series(tenant, index=bad.index)
        for i, rec in zip(bad.index, raw.to_dict("records"), strict=True):
            self.rejects.append({"tenant_id": int(ten.loc[i]) if pd.notna(ten.loc[i]) else NETWORK_TENANT,
                                 "intake_file_id": file_id, "feed_date": self.feed_date, "file_name": name,
                                 "row_number": int(i) + 1, "reject_reason": bad.loc[i],
                                 "raw_json": json.dumps(rec, default=str, sort_keys=True)})


def _file_row(fid: int, feed_date: str, seq: int, name: str, exp: dict, rows: int, digest: str | None, status: str,
              msg: str, at: str) -> pd.DataFrame:
    return pd.DataFrame([{
        "intake_file_id": fid, "tenant_id": NETWORK_TENANT, "feed_date": feed_date, "sequence_no": seq,
        "file_name": name, "expected_rows": exp.get("row_count"), "actual_rows": rows,
        "expected_sha256": exp.get("sha256"), "actual_sha256": digest, "status": status, "message": msg,
        "processed_at": at, "created_at": at, "updated_at": at}])


def _check_file(w: _Folder, key: str, manifest: dict) -> tuple[int, pd.DataFrame | None, FileResult]:
    name = FILES[key]
    path = w.folder / name
    exp = manifest.get("files", {}).get(name, {})
    fid = int(w.ids.take("intake_file", 1)[0])
    if not path.exists():
        res = FileResult(w.feed_date, name, "MISSING", message="file listed in schedule but not delivered")
        ac.insert(w.conn, "intake_file", _file_row(fid, w.feed_date, manifest.get("sequence_no", 0), name, exp, 0,
                                                    None, res.status, res.message, w.processed_at))
        return fid, None, res
    df = read_rows(path)
    digest = sha256(path)
    seen = w.conn.execute("select feed_date from intake_file where actual_sha256 = ? and status = 'LOADED'",
                          [digest]).fetchone()
    tc = next(iter(load_contract(CONTRACTS[key]).values()))
    missing = [c.name for c in tc.columns if c.required and c.name not in df.columns]
    if digest != exp.get("sha256"):
        status, msg = "REJECTED", "checksum_mismatch"
    elif missing:
        status, msg = "REJECTED", f"schema_violation: required columns missing {missing}"
    elif len(df) != exp.get("row_count"):
        status, msg = "REJECTED", f"row_count_mismatch: manifest {exp.get('row_count')} vs file {len(df)}"
    elif seen:
        status, msg = "DUPLICATE_FILE", f"duplicate_file: identical to the file loaded on {seen[0]}"
    else:
        status, msg = "LOADED", ""
    ac.insert(w.conn, "intake_file", _file_row(fid, w.feed_date, manifest.get("sequence_no", 0), name, exp, len(df),
                                                digest, status, msg, w.processed_at))
    res = FileResult(w.feed_date, name, status, rows=len(df), message=msg)
    if status != "LOADED":
        w.reject(fid, name, df, pd.Series(msg.split(":")[0], index=df.index))
        res.rejected = len(df)
        return fid, None, res
    return fid, df, res


def _contract_reasons(df: pd.DataFrame, key: str) -> tuple[pd.DataFrame, pd.Series]:
    """Row-level data-contract check; returns (typed valid rows, reason per input row)."""
    tc = next(iter(load_contract(CONTRACTS[key]).values()))
    checked = validate(df, tc)
    for msg in checked.warnings:
        log.warning("%s %s: %s", key, df.attrs.get("feed_date", ""), msg)
    return checked.valid, pd.Series(checked.row_reasons.to_numpy(), index=df.index)


def promote_deals(w: _Folder, fid: int, df: pd.DataFrame, ref: Ref, res: FileResult) -> None:
    _, reasons = _contract_reasons(df, "deals")
    dup_file = df["deal_id"].duplicated(keep="first")
    reasons[dup_file & (reasons == "")] = "duplicate_deal_in_file"
    known_deals = set(pd.read_sql("select deal_id from contract where deal_id is not null", w.conn)["deal_id"])
    known_numbers = set(pd.read_sql("select contract_number from contract", w.conn)["contract_number"])
    ok = reasons == ""
    reasons[ok & df["deal_id"].isin(known_deals)] = "duplicate_deal"
    ok = reasons == ""
    reasons[ok & df["contract_number"].isin(known_numbers)] = "duplicate_contract_number"
    ok = reasons == ""
    typed = df.copy()
    tc = next(iter(load_contract(CONTRACTS["deals"]).values()))
    for col in tc.columns:
        if col.type in ("integer", "number") and col.name in typed:
            typed[col.name] = pd.to_numeric(typed[col.name], errors="coerce")
    rated, biz = rate_deals(typed[ok], ref) if ok.any() else (df.iloc[0:0], pd.Series(dtype=str))
    reasons.loc[biz.index] = biz
    good = rated[biz == ""].copy() if len(rated) else rated
    tenant = df["dealer_number"].map(ref.dealer.set_index("dealer_number")["tenant_id"])
    w.reject(fid, FILES["deals"], df, reasons, tenant)
    if len(good):
        good["_booked_at"] = w.processed_at
        for k, v in sale_rows(good, ref, w.ids, np.datetime64(w.feed_date)).items():
            ac.insert(w.conn, k, v)
    res.promoted, res.rejected = len(good), int((reasons != "").sum())
    res.reasons = reasons[reasons != ""].str.split(":").str[0].value_counts().to_dict()


def _contracts_by_number(conn: sqlite3.Connection, numbers) -> pd.DataFrame:
    numbers = list(dict.fromkeys(n for n in numbers if n))
    if not numbers:
        return pd.DataFrame(columns=["contract_id", "contract_number", "tenant_id", "dealer_id", "status"])
    rows = []
    for i in range(0, len(numbers), 900):
        chunk = numbers[i : i + 900]
        rows.append(pd.read_sql(
            f"select c.contract_id, c.contract_number, c.tenant_id, c.dealer_id, c.status, d.dealer_number "
            f"from contract c join dealer d on d.dealer_id = c.dealer_id "
            f"where c.contract_number in ({','.join('?' * len(chunk))})", conn, params=chunk))
    return pd.concat(rows, ignore_index=True).set_index("contract_number", drop=False)


def promote_events(w: _Folder, fid: int, df: pd.DataFrame, res: FileResult) -> None:
    _, reasons = _contract_reasons(df, "econtract_events")
    reasons[(reasons == "") & df["event_id"].duplicated(keep="first")] = "duplicate_event"
    voids = df[(reasons == "") & (df["event_type"] == "VOIDED")]
    c = _contracts_by_number(w.conn, voids["contract_number"])
    status = voids["contract_number"].map(c["status"]) if len(voids) else pd.Series(dtype=object)
    reasons.loc[voids.index[status.isna().to_numpy()]] = "unknown_contract"
    reasons.loc[voids.index[status.eq("INFORCE").to_numpy()]] = "void_after_remittance"
    reasons.loc[voids.index[status.isin(["VOIDED", "DENIED", "CANCELLED", "EXPIRED"]).to_numpy()]] = "not_voidable"
    go = voids[status.eq("PENDING").to_numpy()].drop_duplicates("contract_number")
    tenant = df["contract_number"].map(c["tenant_id"]) if len(c) else NETWORK_TENANT
    w.reject(fid, FILES["econtract_events"], df, reasons, tenant)
    if len(go):
        k = c.loc[go["contract_number"]]
        at = go["event_at"].to_numpy()
        ids = k["contract_id"].astype(int).tolist()
        ac.update(w.conn, "contract", ids, {"status": "VOIDED"}, w.processed_at)
        ac.insert(w.conn, "contract_status_history", pd.DataFrame({
            "status_history_id": w.ids.take("contract_status_history", len(k)), "tenant_id": k["tenant_id"].values,
            "contract_id": ids, "changed_at": at, "from_status": "PENDING", "to_status": "VOIDED",
            "created_at": w.processed_at, "updated_at": w.processed_at}))
        ph = ",".join(map(str, ids))
        txn = [r[0] for r in w.conn.execute(
            f'select txn_id from "transaction" where txn_type = \'SALE\' and contract_id in ({ph})')]
        if txn:
            tph = ",".join(map(str, txn))
            for table, key in (("transaction", "txn_id"), ("disbursement", "disbursement_id"),
                               ("commission", "commission_id")):
                keys = [r[0] for r in w.conn.execute(f'select {key} from "{table}" where txn_id in ({tph})')]
                ac.soft_delete(w.conn, table, keys, w.processed_at)
    res.promoted, res.rejected = len(go), int((reasons != "").sum())
    res.reasons = reasons[reasons != ""].str.split(":").str[0].value_counts().to_dict()


def promote_remittance(w: _Folder, fid: int, df: pd.DataFrame, res: FileResult) -> None:
    _, reasons = _contract_reasons(df, "remittance")
    reasons[(reasons == "") & df.duplicated(["remit_batch_id", "contract_number"], keep="first")] = "duplicate_row"
    c = _contracts_by_number(w.conn, df["contract_number"])
    status = df["contract_number"].map(c["status"]) if len(c) else pd.Series(None, index=df.index, dtype=object)
    ok = reasons == ""
    reasons[ok & status.isna()] = "unknown_contract"
    ok = reasons == ""
    reasons[ok & status.isin(["VOIDED", "DENIED"])] = "contract_not_remittable"
    ok = reasons == ""
    reasons[ok & status.notna() & ~status.eq("PENDING")] = "duplicate_remittance"
    ok = reasons == ""
    dealer = df["contract_number"].map(c["dealer_number"]) if len(c) else status
    reasons[ok & (dealer != df["dealer_number"])] = "dealer_mismatch"
    ok = reasons == ""
    reasons[ok & df.loc[ok, "contract_number"].duplicated(keep="first").reindex(df.index, fill_value=False)] = (
        "duplicate_remittance"
    )
    tenant = df["contract_number"].map(c["tenant_id"]) if len(c) else NETWORK_TENANT
    w.reject(fid, FILES["remittance"], df, reasons, tenant)
    good = df[reasons == ""]
    if len(good):
        k = c.loc[good["contract_number"]]
        r = pd.DataFrame({
            "remit_batch_id": good["remit_batch_id"].values, "tenant_id": k["tenant_id"].values,
            "dealer_id": k["dealer_id"].values, "contract_id": k["contract_id"].values,
            "amount": pd.to_numeric(good["remit_amount"]).values,
            "entered_at": (good["entered_date"].fillna(good["remit_date"]) + " 09:00:00").values,
            "sent_at": (good["remit_date"] + " 10:00:00").values, "received_at": w.processed_at,
            "source": "NETWORK"})
        rows = remit_rows(r, w.ids)
        batches = rows.pop("remit_batch")
        have = set(pd.read_sql("select remit_batch_id from remit_batch", w.conn)["remit_batch_id"])
        ac.insert(w.conn, "remit_batch", batches[~batches["remit_batch_id"].isin(have)])
        for k2, v in rows.items():
            ac.insert(w.conn, k2, v)
        ac.update(w.conn, "contract", k["contract_id"].astype(int).tolist(), {"status": "INFORCE"}, w.processed_at)
    res.promoted, res.rejected = len(good), int((reasons != "").sum())
    res.reasons = reasons[reasons != ""].str.split(":").str[0].value_counts().to_dict()


def process_folder(conn: sqlite3.Connection, folder: Path, ref: Ref | None = None) -> list[FileResult]:
    """Validate and promote one landing folder inside a single transaction."""
    ref = ref or Ref.load(conn)
    w = _Folder(conn, folder)
    try:
        manifest = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        manifest = {"files": {}, "sequence_no": 0}
        log.error("%s: unreadable manifest (%s): every file is rejected", folder.name, exc)
    conn.execute("BEGIN")
    try:
        for key, fn in (("deals", promote_deals), ("econtract_events", promote_events),
                        ("remittance", promote_remittance)):
            fid, df, res = _check_file(w, key, manifest)
            if df is not None:
                df.attrs["feed_date"] = w.feed_date
                fn(w, fid, df, ref, res) if key == "deals" else fn(w, fid, df, res)
            w.results.append(res)
        if w.rejects:
            rej = pd.DataFrame(w.rejects)
            rej.insert(0, "reject_id", w.ids.take("intake_reject", len(rej)))
            rej["created_at"] = rej["updated_at"] = w.processed_at
            ac.insert(conn, "intake_reject", rej)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return w.results


def run(admin_core: Path, landing: Path, only: str | None = None) -> list[FileResult]:
    if not admin_core.exists():
        raise FileNotFoundError(f"Admin Core not found: {admin_core} (run `python -m generator`)")
    results: list[FileResult] = []
    with sqlite3.connect(admin_core, isolation_level=None) as conn:
        ref = Ref.load(conn)
        for folder in pending_folders(conn, landing, only):
            for r in process_folder(conn, folder, ref):
                log.info("%s %-24s %-14s rows=%d promoted=%d rejected=%d %s %s", r.feed_date, r.file_name, r.status,
                         r.rows, r.promoted, r.rejected, r.message, r.reasons or "")
                results.append(r)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.feed", description=__doc__.split("\n\n")[0])
    ap.add_argument("--date", help="process only this landing folder (YYYY-MM-DD)")
    ap.add_argument("--landing", type=Path, default=LANDING_DIR)
    ap.add_argument("--admin-core", type=Path, default=ADMIN_CORE_PATH)
    ap.add_argument("--json", action="store_true", help="print the per-file results as JSON")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    t0 = time.perf_counter()
    results = run(args.admin_core, args.landing, args.date)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    folders = len({r.feed_date for r in results})
    log.info("processed %d folder(s), %d file(s) in %.1fs; rejected files: %d, rejected rows: %d", folders,
             len(results), time.perf_counter() - t0, sum(r.status != "LOADED" for r in results),
             sum(r.rejected for r in results))
    if args.date and not results:
        log.warning("no unprocessed landing folder for %s", args.date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
