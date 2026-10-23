"""CRM / call-center export and admin-API page drops -> ``raw_crm`` and ``raw_admin_api``.

Both land like the dealer feed: raw keeps every delivered row (as text, with lineage columns), a
manifest/extract table records the file-level checks, and rows that break the data contract go to
``_quarantine`` with ``reject_reason`` and ``raw_json``. Staging removes quarantined rows by
anti-joining on (``_source_file``, ``_row_number``).

- ``raw_crm.calls`` / ``raw_crm._manifest`` / ``raw_crm._quarantine`` (contracts/crm_calls.yml)
- ``raw_admin_api.dealer`` / ``raw_admin_api._extract`` / ``raw_admin_api._quarantine``
  (contracts/admin_api_dealer.yml)

Each loaded drop is logged in ``audit.ingest_drop`` and a drop loads when its sequence number is
not in that set, so a drop is loaded exactly once, a re-run loads nothing, and a late or repaired
drop still loads after a later one (``diag_freshness`` fails while one is missing). A
rejected file still counts as loaded (the producer re-sends a fresh export; the reject is visible
in the manifest table and in quarantine). The watermark (highest sequence loaded) is for monitoring.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from generator.intake import API_ENTITY, CALLS_FILE, MANIFEST
from generator.landing import sha256
from ingest import CONTRACTS_DIR, raw
from ingest.contract import load_contract, validate

log = logging.getLogger("ingest.intake")
CRM_CONTRACT = CONTRACTS_DIR / "crm_calls.yml"
API_CONTRACT = CONTRACTS_DIR / "admin_api_dealer.yml"
LINEAGE = {"_source_file": "VARCHAR", "_file_sha256": "VARCHAR", "_row_number": "BIGINT", "_feed_date": "DATE"}
QUARANTINE_COLS = {"file_name": "VARCHAR", "reject_reason": "VARCHAR", "raw_json": "VARCHAR", **LINEAGE}
CRM_MANIFEST_COLS = {"export_date": "DATE", "sequence_no": "BIGINT", "export_type": "VARCHAR", "file_name": "VARCHAR",
                     "expected_rows": "BIGINT", "actual_rows": "BIGINT", "expected_sha256": "VARCHAR",
                     "actual_sha256": "VARCHAR", "checksum_ok": "BOOLEAN", "row_count_ok": "BOOLEAN",
                     "rows_valid": "BIGINT", "rows_quarantined": "BIGINT"}
API_EXTRACT_COLS = {"extract_date": "DATE", "sequence_no": "BIGINT", "entity": "VARCHAR", "page_count": "BIGINT",
                    "pages_received": "BIGINT", "total_records": "BIGINT", "records_received": "BIGINT",
                    "is_complete": "BOOLEAN", "rows_valid": "BIGINT", "rows_quarantined": "BIGINT"}


def _folders(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def _row_reasons(df: pd.DataFrame, contract_path: Path) -> pd.Series:
    """Contract reasons per row ('' = valid), input order."""
    tc = next(iter(load_contract(contract_path).values()))
    return validate(df, tc).row_reasons.reindex(range(len(df))).fillna("")


def _quarantine_frame(df: pd.DataFrame, reasons: pd.Series, rel: str, digest: str, feed_date: str) -> pd.DataFrame:
    bad = reasons[reasons != ""]
    rows = df.loc[bad.index].astype(object).where(df.loc[bad.index].notna(), None)
    return pd.DataFrame({
        "file_name": rel.split("/", 1)[-1], "reject_reason": bad.to_numpy(),
        "raw_json": [json.dumps(r, default=str, sort_keys=True) for r in rows.to_dict("records")],
        "_source_file": rel, "_file_sha256": digest, "_row_number": (bad.index + 1).to_numpy(), "_feed_date": feed_date,
    })


def _with_lineage(df: pd.DataFrame, rel: str, digest: str, feed_date: str, row_numbers=None) -> pd.DataFrame:
    meta = pd.DataFrame({"_source_file": rel, "_file_sha256": digest,
                         "_row_number": range(1, len(df) + 1) if row_numbers is None else row_numbers,
                         "_feed_date": feed_date})
    return pd.concat([df.reset_index(drop=True), meta.reset_index(drop=True)], axis=1)


# --------------------------------------------------------------------------- CRM
def load_crm(con: duckdb.DuckDBPyConnection, intake: Path, run: raw.Run) -> list[raw.TableResult]:
    source, schema = "crm", raw.RAW_SCHEMAS["crm"]
    res = {k: raw.TableResult(source, k) for k in ("calls", "_manifest", "_quarantine")}
    since = int(raw.get_watermark(con, source, "calls") or 0)
    for r in res.values():
        r.watermark_from = r.watermark_to = str(since) if since else None
    raw.ensure_table(con, schema, "_manifest", CRM_MANIFEST_COLS)
    raw.ensure_table(con, schema, "_quarantine", QUARANTINE_COLS)
    tc = next(iter(load_contract(CRM_CONTRACT).values()))
    loaded = raw.loaded_drops(con, source, f"SELECT DISTINCT strftime(export_date, '%Y-%m-%d'), sequence_no "
                                           f"FROM {schema}._manifest")
    for folder in _folders(intake / "crm"):
        try:
            m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
            seq = int(m["sequence_no"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.error("crm %s: missing or unreadable manifest; retried on every run until it is fixed", folder.name)
            raw.record_drop(con, source, folder.name, None, None, "unreadable", run)
            continue
        if seq in loaded:
            continue
        if not (folder / CALLS_FILE).exists():
            log.error("crm %s: manifest without %s; retried on every run", folder.name, CALLS_FILE)
            raw.record_drop(con, source, folder.name, seq, None, "unreadable", run)
            continue
        loaded.add(seq)
        path, rel = folder / CALLS_FILE, f"crm/{folder.name}/{CALLS_FILE}"
        df = pd.read_csv(path, dtype=str, keep_default_na=False).replace("", None)
        digest, exp = sha256(path), m.get("files", {}).get(CALLS_FILE, {})
        checksum_ok, count_ok = digest == exp.get("sha256"), len(df) == exp.get("row_count")
        reasons = _row_reasons(df, CRM_CONTRACT)
        no_key = df.get("contract_number").isna() & df.get("phone_hash").isna()
        reasons[no_key.to_numpy()] = (reasons[no_key.to_numpy()] + ";no_customer_key").str.lstrip(";")
        dup = df["call_id"].duplicated(keep="first").to_numpy()
        reasons[dup] = (reasons[dup] + ";duplicate_call_id").str.lstrip(";")
        if not (checksum_ok and count_ok):
            file_reason = "checksum_mismatch" if not checksum_ok else "row_count_mismatch"
            reasons = pd.Series(file_reason, index=reasons.index)
        cols = {**{c.name: "VARCHAR" for c in tc.columns}, **LINEAGE}
        raw.ensure_table(con, schema, "calls", cols)
        res["calls"].rows_read += len(df)
        res["calls"].rows_loaded += raw.append(con, schema, "calls", _with_lineage(df, rel, digest, folder.name),
                                               cols, run, source, seq, False)
        q = _quarantine_frame(df, reasons, rel, digest, folder.name)
        res["_quarantine"].rows_read += len(q)
        res["_quarantine"].rows_loaded += raw.append(con, schema, "_quarantine", q, QUARANTINE_COLS, run, source,
                                                     seq, False)
        res["calls"].rows_quarantined += len(q)
        man = pd.DataFrame([{
            "export_date": folder.name, "sequence_no": seq, "export_type": m.get("export_type"),
            "file_name": CALLS_FILE, "expected_rows": exp.get("row_count"), "actual_rows": len(df),
            "expected_sha256": exp.get("sha256"), "actual_sha256": digest, "checksum_ok": checksum_ok,
            "row_count_ok": count_ok, "rows_valid": int((reasons == "").sum()), "rows_quarantined": len(q)}])
        res["_manifest"].rows_read += 1
        res["_manifest"].rows_loaded += raw.append(con, schema, "_manifest", man, CRM_MANIFEST_COLS, run, source, seq,
                                                   False)
        raw.record_drop(con, source, folder.name, seq, sha256(folder / MANIFEST), "loaded", run)
        for r in res.values():
            r.watermark_to = str(max(seq, int(r.watermark_to or 0)))
    if res["calls"].watermark_to != res["calls"].watermark_from:
        raw.set_watermark(con, source, "calls", "sequence_no", res["calls"].watermark_to, run)
    return list(res.values())


# --------------------------------------------------------------------------- admin API
def load_admin_api(con: duckdb.DuckDBPyConnection, intake: Path, run: raw.Run) -> list[raw.TableResult]:
    source, schema = "admin_api", raw.RAW_SCHEMAS["admin_api"]
    res = {k: raw.TableResult(source, k) for k in (API_ENTITY, "_extract", "_quarantine")}
    since = int(raw.get_watermark(con, source, API_ENTITY) or 0)
    for r in res.values():
        r.watermark_from = r.watermark_to = str(since) if since else None
    tc = next(iter(load_contract(API_CONTRACT).values()))
    cols = {**{c.name: "VARCHAR" for c in tc.columns}, "_page": "BIGINT", **LINEAGE}
    raw.ensure_table(con, schema, API_ENTITY, cols)
    raw.ensure_table(con, schema, "_extract", API_EXTRACT_COLS)
    raw.ensure_table(con, schema, "_quarantine", QUARANTINE_COLS)
    loaded = raw.loaded_drops(con, source, f"SELECT DISTINCT strftime(extract_date, '%Y-%m-%d'), sequence_no "
                                           f"FROM {schema}._extract")
    for day in _folders(intake / "admin_api"):
        pages = sorted((day / API_ENTITY).glob("page_*.json"))
        if not pages:
            continue
        try:
            bodies = [json.loads(p.read_text(encoding="utf-8")) for p in pages]
            seq = int(bodies[0]["sequence_no"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.error("admin_api %s: unreadable page; retried on every run until it is fixed", day.name)
            raw.record_drop(con, source, day.name, None, None, "unreadable", run)
            continue
        if seq in loaded:
            continue
        loaded.add(seq)
        page_count, total = int(bodies[0].get("page_count", 0)), int(bodies[0].get("total_records", 0))
        got = {int(b["page"]) for b in bodies}
        frames = []
        for p, b in zip(pages, bodies, strict=True):
            df = pd.DataFrame(b.get("records", []), columns=list(tc.column_names)).astype(object)
            df = df.map(lambda v: None if v is None or (isinstance(v, float) and v != v) else str(v))
            rel = f"admin_api/{day.name}/{API_ENTITY}/{p.name}"
            f = _with_lineage(df, rel, sha256(p), day.name)
            f["_page"] = int(b["page"])
            frames.append(f)
        df = pd.concat(frames, ignore_index=True)
        complete = got == set(range(1, page_count + 1)) and len(df) == total
        reasons = _row_reasons(df[tc.column_names], API_CONTRACT)
        if not complete:
            reasons = pd.Series("incomplete_extract", index=reasons.index)
        res[API_ENTITY].rows_read += len(df)
        res[API_ENTITY].rows_loaded += raw.append(con, schema, API_ENTITY, df, cols, run, source, seq, False)
        bad = reasons[reasons != ""]
        rows = df.loc[bad.index, tc.column_names]
        q = pd.DataFrame({
            "file_name": df.loc[bad.index, "_source_file"].str.split("/", n=1).str[-1].to_numpy(),
            "reject_reason": bad.to_numpy(),
            "raw_json": [json.dumps(r, default=str, sort_keys=True) for r in rows.to_dict("records")],
            **{c: df.loc[bad.index, c].to_numpy() for c in LINEAGE}})
        res["_quarantine"].rows_read += len(q)
        res["_quarantine"].rows_loaded += raw.append(con, schema, "_quarantine", q, QUARANTINE_COLS, run, source,
                                                     seq, False)
        res[API_ENTITY].rows_quarantined += len(q)
        ext = pd.DataFrame([{
            "extract_date": day.name, "sequence_no": seq, "entity": API_ENTITY, "page_count": page_count,
            "pages_received": len(got), "total_records": total, "records_received": len(df), "is_complete": complete,
            "rows_valid": int((reasons == "").sum()), "rows_quarantined": len(q)}])
        res["_extract"].rows_read += 1
        res["_extract"].rows_loaded += raw.append(con, schema, "_extract", ext, API_EXTRACT_COLS, run, source, seq,
                                                  False)
        raw.record_drop(con, source, day.name, seq, sha256(pages[0]), "loaded", run)
        for r in res.values():
            r.watermark_to = str(max(seq, int(r.watermark_to or 0)))
    if res[API_ENTITY].watermark_to != res[API_ENTITY].watermark_from:
        raw.set_watermark(con, source, API_ENTITY, "sequence_no", res[API_ENTITY].watermark_to, run)
    return list(res.values())


def run(con: duckdb.DuckDBPyConnection, intake: Path, run_: raw.Run, sources: tuple[str, ...]) -> list[raw.TableResult]:
    started = datetime.now()
    results: list[raw.TableResult] = []
    for name, loader in (("crm", load_crm), ("admin_api", load_admin_api)):
        if name not in sources:
            continue
        con.execute("BEGIN")
        try:
            part = loader(con, intake, run_)
            con.execute("COMMIT")
        except (OSError, duckdb.Error, ValueError, KeyError) as exc:
            con.execute("ROLLBACK")
            log.error("%s intake failed: %s", name, exc)
            part = [raw.TableResult(name, "files", status="error", message=str(exc))]
        results += part
    for r in results:
        raw.log_run(con, run_, r, started)
    return results
