"""Reconciliation gate: ``python -m pipeline.reconcile [--warehouse PATH]``.

For each source and entity it compares the row count and the dollar total at three points:

    source DB (SQLite)  ->  raw (latest version, not deleted)  ->  core marts

    source == raw + quarantined (rows ingest could not accept and staging could not replay)
    raw    == marts + excluded  (documented exclusions: demo dealers / test ranges, from
                                 intermediate.int_reportable_scope; converted legacy claims)

The two file intakes (CRM call-center export, admin-API dealer extract) have no source database: their
"source" is what the producer delivered, i.e. the rows each file / extract actually carried, as recorded
by ingest in ``raw_crm._manifest`` and ``raw_admin_api._extract`` (``source_db = "warehouse"``). The
admin-API row is checked on the newest extract (each extract is a full roster).

It also reads the migration parallel run (``core.mart_migration_parallel_run``): the same claims
and contracts counted in the legacy system and in Admin Core at the cutover. A migrated entity that
does not tie after its documented exclusion is a break; an entity the source has not converted yet
is recorded as ``not_migrated`` (visible in ``audit.reconciliation``, not a break).

Any difference above $0.01 or 0 rows is a break. It also runs the in-warehouse calibration tests
tagged ``reconciliation`` (``dbt/tests/reconciliation/``: the dealer-cost split ties to every
transaction, claim roll-ups tie to the one claims fact, earned <= written, raw -> core counts,
loss-ratio math, call-center roll-ups tie to the contact fact, and dim_dealer agrees with the admin-API
dealer roster: ``recon_dealer_roster_drift``). Results go to ``audit.reconciliation``; the exit code is 1 on any break.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from generator import ADMIN_CORE_PATH, LEGACY_DB_PATH
from pipeline.diagnostics import (
    WAREHOUSE_PATH,
    GateError,
    Runner,
    report,
    run_dbt_gate,
    run_id,
    subprocess_runner,
)

log = logging.getLogger("pipeline.reconcile")

ROW_TOLERANCE = 0
DOLLAR_TOLERANCE = 0.01

# Legacy keeps duplicate rows and no primary keys: take the latest row per business key.
_LEGACY_LATEST = """
    select * from (
        select *, row_number() over (partition by {key} order by updated_at desc) as _rn from {table}
    ) where _rn = 1 and coalesce(removed_from_source, 0) = 0
"""


@dataclass(frozen=True)
class Entity:
    """One reconciled entity. Every query returns (row_count, dollar_total)."""

    source_system: str
    entity: str
    measure: str  # what the dollar total sums
    source_sql: str  # against the source SQLite database
    raw_sql: str  # against the warehouse: raw latest versions (staging), not deleted
    mart_sql: str  # against the warehouse: core marts
    excluded_sql: str  # rows/dollars removed by documented rules between raw and marts
    quarantined_sql: str = "select 0, 0"  # rows/dollars that never reached raw (ingest quarantine)
    source_db: str = ""  # connection that source_sql runs on; "" = source_system, "warehouse" = the warehouse


def _scope(alias: str = "x") -> str:
    return (f"{alias}.contract_key in (select contract_key from intermediate.int_reportable_scope "
            "where not is_reportable)")


# Admin-API extract: the newest delivery, and its rows that ingest did not quarantine.
_API_LATEST = "extract_date = (select max(extract_date) from raw_admin_api._extract)"
_API_LATEST_FEED = "_feed_date = (select max(extract_date) from raw_admin_api._extract)"
_API_ACCEPTED = ("not exists (select 1 from raw_admin_api._quarantine q where q._source_file = r._source_file "
                 "and q._row_number = r._row_number)")

# Claim status buckets, the same rule as core.dim_claim.status_group (legacy statuses are already
# lower-case paid / denied / open in the source, so the legacy rule maps them one to one).
STATUS_GROUPS: tuple[str, ...] = ("denied", "open", "paid")
_AC_STATUS_GROUP = ("case when {col} = 'DENIED' then 'denied' when {col} like 'CLOSED%' then 'paid' "
                    "else 'open' end")
_LG_STATUS_GROUP = "case lower({col}) when 'paid' then 'paid' when 'denied' then 'denied' else 'open' end"


def _claim_header_entities() -> tuple[Entity, ...]:
    """Claim headers per system and status group: count and requested dollars, source -> staging ->
    core.dim_claim. Every claim is tied, not only the ones with payment lines, so a join or filter that
    drops open, pending or denied claims breaks its own bucket. Exclusions are claims on non-reportable
    contracts (int_reportable_scope); converted legacy claims are counted once, on the Admin Core side."""
    out = []
    for g in STATUS_GROUPS:
        ac = _AC_STATUS_GROUP.format(col="status")
        out.append(Entity(
            "admin_core", f"claim headers ({g})", "requested amount",
            f"select count(*), coalesce(sum(requested_amount), 0) from claim "
            f"where removed_from_source = 0 and {ac} = '{g}'",
            f"select count(*), coalesce(sum(requested_amount), 0) from staging.stg_admin_core__claim "
            f"where not is_deleted and {ac} = '{g}'",
            f"select count(*), coalesce(sum(requested_amount), 0) from core.dim_claim "
            f"where source_system = 'admin_core' and status_group = '{g}'",
            f"select count(*), coalesce(sum(x.requested_amount), 0) from intermediate.int_claims_conformed x "
            f"where x.source_system = 'admin_core' and {_AC_STATUS_GROUP.format(col='x.status')} = '{g}' "
            f"and {_scope()}",
        ))
        out.append(Entity(
            "legacy_admin", f"claim headers ({g}, not converted)", "requested amount",
            "select count(*), coalesce(sum(abs(amount_requested)), 0) from ("
            + _LEGACY_LATEST.format(key="ltrim(claim_no, '0')", table="claims")
            + f") where {_LG_STATUS_GROUP.format(col='status')} = '{g}' and ltrim(claim_no, '0') not in "
            "(select claim_number from core.claim where removed_from_source = 0)",
            f"select count(*), coalesce(sum(amount_requested), 0) from staging.stg_legacy_admin__claims "
            f"where not is_deleted and {_LG_STATUS_GROUP.format(col='claim_status')} = '{g}' and claim_number "
            "not in (select claim_number from staging.stg_admin_core__claim where not is_deleted)",
            f"select count(*), coalesce(sum(requested_amount), 0) from core.dim_claim "
            f"where source_system = 'legacy_admin' and status_group = '{g}'",
            f"select count(*), coalesce(sum(x.requested_amount), 0) from intermediate.int_claims_conformed x "
            f"where x.source_system = 'legacy_admin' and {_AC_STATUS_GROUP.format(col='x.status')} = '{g}' "
            f"and {_scope()}",
        ))
    return tuple(out)


ENTITIES: tuple[Entity, ...] = (
    Entity(
        "admin_core", "contracts", "coverage_cost",
        "select count(*), coalesce(sum(coverage_cost), 0) from contract where removed_from_source = 0",
        "select count(*), coalesce(sum(coverage_cost), 0) from staging.stg_admin_core__contract where not is_deleted",
        "select count(*), coalesce(sum(f.coverage_cost), 0) from core.dim_contract k "
        "join core.dim_contract_finance f using (contract_key) where k.source_system = 'admin_core'",
        f"select count(*), coalesce(sum(x.coverage_cost), 0) from intermediate.int_contracts_conformed x "
        f"where x.source_system = 'admin_core' and {_scope()}",
    ),
    Entity(
        "admin_core", "sales", "SALE transaction amount",
        "select count(*), coalesce(sum(amount), 0) from \"transaction\" "
        "where txn_type = 'SALE' and removed_from_source = 0",
        "select count(*), coalesce(sum(amount), 0) from staging.stg_admin_core__transaction "
        "where txn_type = 'SALE' and not is_deleted",
        "select count(*), coalesce(sum(amount), 0) from core.fct_transaction "
        "where source_system = 'admin_core' and txn_type = 'SALE'",
        f"select count(*), coalesce(sum(x.amount), 0) from intermediate.int_transactions_posted x "
        f"where x.source_system = 'admin_core' and x.txn_type = 'SALE' and {_scope()}",
    ),
    Entity(
        "admin_core", "claim payment lines", "claims paid",
        "select count(*), coalesce(sum(l.paid_amount), 0) from claim_payment_line l "
        "join claim c using (claim_id) where l.removed_from_source = 0 and c.removed_from_source = 0",
        "select count(*), coalesce(sum(l.paid_amount), 0) from staging.stg_admin_core__claim_payment_line l "
        "join staging.stg_admin_core__claim c using (claim_id) where not l.is_deleted and not c.is_deleted",
        "select count(*), coalesce(sum(paid_amount), 0) from core.fct_claim_payment_line "
        "where source_system = 'admin_core'",
        f"select count(*), coalesce(sum(x.paid_amount), 0) from intermediate.int_claim_lines_conformed x "
        f"where x.source_system = 'admin_core' and {_scope()}",
    ),
    Entity(
        "admin_core", "claim pending (all lines)", "pending amount",
        "select count(*), coalesce(sum(l.pending_amount), 0) from claim_payment_line l "
        "join claim c using (claim_id) where l.removed_from_source = 0 and c.removed_from_source = 0",
        "select count(*), coalesce(sum(l.pending_amount), 0) from staging.stg_admin_core__claim_payment_line l "
        "join staging.stg_admin_core__claim c using (claim_id) where not l.is_deleted and not c.is_deleted",
        "select count(*), coalesce(sum(pending_amount), 0) from core.fct_claim_payment_line "
        "where source_system = 'admin_core'",
        f"select count(*), coalesce(sum(x.pending_amount), 0) from intermediate.int_claim_lines_conformed x "
        f"where x.source_system = 'admin_core' and {_scope()}",
    ),
    *_claim_header_entities(),
    Entity(
        "admin_core", "cancellations", "customer refunds",
        "select count(*), coalesce(sum(customer_refund), 0) from cancellation where removed_from_source = 0",
        "select count(*), coalesce(sum(customer_refund), 0) from staging.stg_admin_core__cancellation "
        "where not is_deleted",
        "select count(*), coalesce(sum(customer_refund), 0) from core.fct_cancellation "
        "where source_system = 'admin_core'",
        f"select count(*), coalesce(sum(x.customer_refund), 0) from intermediate.int_cancellations_conformed x "
        f"where x.source_system = 'admin_core' and {_scope()}",
    ),
    Entity(
        "admin_core", "cash receipts", "remitted cash",
        "select count(*), coalesce(sum(amount), 0) from cash_transaction where removed_from_source = 0",
        "select count(*), coalesce(sum(amount), 0) from staging.stg_admin_core__cash_transaction where not is_deleted",
        "select count(*), coalesce(sum(amount), 0) from core.fct_cash_transaction",
        f"select count(*), coalesce(sum(x.amount), 0) from intermediate.int_remittance_lifecycle x where {_scope()}",
    ),
    Entity(
        "legacy_admin", "contracts", "dealer cost",
        "select count(*), coalesce(sum(abs(dealer_cost)), 0) from ("
        + _LEGACY_LATEST.format(key="legacy_contract_id", table="contracts") + ")",
        "select count(*), coalesce(sum(dealer_cost), 0) from staging.stg_legacy_admin__contracts where not is_deleted",
        "select count(*), coalesce(sum(f.coverage_cost), 0) from core.dim_contract k "
        "join core.dim_contract_finance f using (contract_key) where k.source_system = 'legacy_admin'",
        f"select count(*), coalesce(sum(x.coverage_cost), 0) from intermediate.int_contracts_conformed x "
        f"where x.source_system = 'legacy_admin' and {_scope()}",
        # quarantined by ingest and not replayable by staging (the MM/DD/YYYY rows ARE replayed)
        "select count(distinct q.id), coalesce(sum(q.cost), 0) from ("
        "  select distinct cast(raw_json::json ->> 'legacy_contract_id' as bigint) as id,"
        "         abs(cast(raw_json::json ->> 'dealer_cost' as double)) as cost"
        "  from raw_legacy_admin._quarantine where table_name = 'contracts') q "
        "where q.id not in (select legacy_contract_id from staging.stg_legacy_admin__contracts)",
    ),
    Entity(
        "legacy_admin", "claims (not converted)", "claims paid",
        "select count(*), coalesce(sum(case when lower(status) = 'paid' then abs(amount_paid) else 0 end), 0) from ("
        + _LEGACY_LATEST.format(key="ltrim(claim_no, '0')", table="claims")
        + ") where ltrim(claim_no, '0') not in "
        "(select claim_number from core.claim where removed_from_source = 0)",
        "select count(*), coalesce(sum(case when claim_status = 'paid' then amount_paid else 0 end), 0) "
        "from staging.stg_legacy_admin__claims where not is_deleted and claim_number not in "
        "(select claim_number from staging.stg_admin_core__claim where not is_deleted)",
        "select count(*), coalesce(sum(paid_amount), 0) from core.fct_claim_payment_line "
        "where source_system = 'legacy_admin'",
        f"select count(*), coalesce(sum(x.paid_amount), 0) from intermediate.int_claim_lines_conformed x "
        f"where x.source_system = 'legacy_admin' and {_scope()}",
    ),
    Entity(
        "legacy_admin", "cancellations", "customer refunds",
        "select count(*), coalesce(sum(abs(refund_amount)), 0) from ("
        + _LEGACY_LATEST.format(key="cancellation_id", table="cancellations") + ")",
        "select count(*), coalesce(sum(refund_amount), 0) from staging.stg_legacy_admin__cancellations "
        "where not is_deleted",
        "select count(*), coalesce(sum(customer_refund), 0) from core.fct_cancellation "
        "where source_system = 'legacy_admin'",
        f"select count(*), coalesce(sum(x.customer_refund), 0) from intermediate.int_cancellations_conformed x "
        f"where x.source_system = 'legacy_admin' and {_scope()}",
    ),
    Entity(
        "crm", "call-center contacts", "rows only",
        "select coalesce(sum(actual_rows), 0), 0 from raw_crm._manifest",
        "select count(*), 0 from staging.stg_crm__calls",
        "select count(*), 0 from core.fct_call_center_contact",
        "select count(*), 0 from intermediate.int_call_contract_bridge where contract_key in "
        "(select contract_key from intermediate.int_reportable_scope where not is_reportable)",
        # rows that never became a staging row: contract / checksum rejects, plus resends superseded
        # by a later delivery of the same call_id
        "select (select count(*) from raw_crm._quarantine) + (select count(*) from raw_crm.calls r where not exists "
        "(select 1 from raw_crm._quarantine q where q._source_file = r._source_file and (q._row_number = r._row_number "
        "or q._row_number is null or q._row_number = 0))) - (select count(*) from staging.stg_crm__calls), 0",
        source_db="warehouse",
    ),
    Entity(
        "admin_api", "dealer roster (latest extract)", "rows only",
        f"select coalesce(sum(records_received), 0), 0 from raw_admin_api._extract where {_API_LATEST}",
        f"select count(*), 0 from raw_admin_api.dealer r where r.{_API_LATEST_FEED} and {_API_ACCEPTED}",
        f"select count(*), 0 from raw_admin_api.dealer r where r.{_API_LATEST_FEED} and {_API_ACCEPTED} "
        "and upper(trim(r.dealer_number)) in (select dealer_number from core.dim_dealer)",
        f"select count(*), 0 from raw_admin_api.dealer r where r.{_API_LATEST_FEED} and {_API_ACCEPTED} "
        "and upper(trim(r.dealer_number)) in "
        "(select dealer_number from intermediate.int_reportable_dealers where not is_reportable)",
        f"select count(*), 0 from raw_admin_api._quarantine where {_API_LATEST_FEED}",
        source_db="warehouse",
    ),
)

RECONCILIATION_DDL = """
create schema if not exists audit;
create table if not exists audit.reconciliation (
    run_id varchar, source_system varchar, entity varchar, measure varchar, comparison varchar,
    expected_rows bigint, actual_rows bigint, expected_amount double, actual_amount double,
    row_diff bigint, amount_diff double, status varchar, checked_at timestamp
);
"""


@dataclass
class Break:
    source_system: str
    entity: str
    measure: str
    comparison: str  # "source -> raw" | "raw -> marts"
    expected_rows: int
    actual_rows: int
    expected_amount: float
    actual_amount: float
    waiver: str = ""  # documented non-break status (for example "not_migrated")

    @property
    def row_diff(self) -> int:
        return self.actual_rows - self.expected_rows

    @property
    def amount_diff(self) -> float:
        return round(self.actual_amount - self.expected_amount, 6)

    @property
    def ok(self) -> bool:
        if self.waiver:
            return True
        return abs(self.row_diff) <= ROW_TOLERANCE and abs(self.amount_diff) <= DOLLAR_TOLERANCE


def _one(con, sql: str) -> tuple[int, float]:
    n, amount = con.execute(sql).fetchone()
    return int(n or 0), float(amount or 0.0)


def compare(entities: Sequence[Entity], wh: duckdb.DuckDBPyConnection, sources: dict[str, sqlite3.Connection]
            ) -> list[Break]:
    out = []
    for e in entities:
        db = e.source_db or e.source_system
        src = _one(wh if db == "warehouse" else sources[db], e.source_sql)
        raw = _one(wh, e.raw_sql)
        mart = _one(wh, e.mart_sql)
        excl = _one(wh, e.excluded_sql)
        quar = _one(wh, e.quarantined_sql)
        out.append(Break(e.source_system, e.entity, e.measure, "source -> raw",
                         src[0], raw[0] + quar[0], round(src[1], 2), round(raw[1] + quar[1], 2)))
        out.append(Break(e.source_system, e.entity, e.measure, "raw -> marts",
                         raw[0], mart[0] + excl[0], round(raw[1], 2), round(mart[1] + excl[1], 2)))
    return out


PARALLEL_RUN_SQL = """
select entity, legacy_rows, admin_core_rows + excluded_rows, legacy_amount, admin_core_amount + excluded_amount, status
from core.mart_migration_parallel_run order by entity
"""


def parallel_run(wh: duckdb.DuckDBPyConnection) -> list[Break]:
    """Legacy vs Admin Core at cutover, after documented exclusions (empty if the mart is absent)."""
    try:
        rows = wh.execute(PARALLEL_RUN_SQL).fetchall()
    except duckdb.Error:
        log.warning("core.mart_migration_parallel_run not built; parallel run skipped")
        return []
    return [Break("migration", entity, "rows/$", "legacy -> admin_core", int(lr), int(cr), round(float(la), 2),
                  round(float(ca), 2), "not_migrated" if status == "NOT_MIGRATED" else "")
            for entity, lr, cr, la, ca, status in rows]


def open_sources(admin_core: Path, legacy: Path) -> dict[str, sqlite3.Connection]:
    for p in (admin_core, legacy):
        if not p.exists():
            raise GateError(f"source database missing: {p}")
    ac = sqlite3.connect(f"file:{admin_core.as_posix()}?mode=ro", uri=True)
    lg = sqlite3.connect(f"file:{legacy.as_posix()}?mode=ro", uri=True)
    # converted claims are identified by their Admin Core claim number
    lg.execute("attach database ? as core", (f"file:{admin_core.as_posix()}?mode=ro",))
    return {"admin_core": ac, "legacy_admin": lg}


def write_results(con: duckdb.DuckDBPyConnection, rid: str, breaks: Sequence[Break]) -> None:
    con.execute(RECONCILIATION_DDL)
    now = datetime.now(UTC).replace(tzinfo=None)
    con.executemany(
        "insert into audit.reconciliation values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(rid, b.source_system, b.entity, b.measure, b.comparison, b.expected_rows, b.actual_rows,
          b.expected_amount, b.actual_amount, b.row_diff, b.amount_diff, b.waiver or ("pass" if b.ok else "fail"), now)
         for b in breaks],
    )


def run(warehouse: Path = WAREHOUSE_PATH, admin_core: Path = ADMIN_CORE_PATH, legacy: Path = LEGACY_DB_PATH,
        runner: Runner = subprocess_runner) -> tuple[list[Break], list]:
    calibration = run_dbt_gate("tag:reconciliation", "recon_", runner)
    sources = open_sources(admin_core, legacy)
    try:
        with duckdb.connect(str(warehouse)) as con:
            breaks = compare(ENTITIES, con, sources) + parallel_run(con)
            write_results(con, run_id(), breaks)
    finally:
        for s in sources.values():
            s.close()
    return breaks, calibration


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.reconcile", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--admin-core", type=Path, default=ADMIN_CORE_PATH)
    ap.add_argument("--legacy", type=Path, default=LEGACY_DB_PATH)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    os.environ["WAREHOUSE_PATH"] = str(args.warehouse)
    try:
        breaks, calibration = run(args.warehouse, args.admin_core, args.legacy)
    except GateError as exc:
        log.error("reconciliation gate could not run: %s", exc)
        return 1
    for b in breaks:
        log.info("%-12s %-24s %-13s rows %d vs %d, %s %.2f vs %.2f  %s", b.source_system, b.entity, b.comparison,
                 b.expected_rows, b.actual_rows, b.measure, b.expected_amount, b.actual_amount,
                 b.waiver or ("ok" if b.ok else "BREAK"))
    bad = [b for b in breaks if not b.ok]
    if bad:
        log.error("reconciliation: %d break(s) above %d rows / $%.2f", len(bad), ROW_TOLERANCE, DOLLAR_TOLERANCE)
    code = report(calibration, "reconciliation calibration")
    return 1 if bad or code else 0


if __name__ == "__main__":
    raise SystemExit(main())
