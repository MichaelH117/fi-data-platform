"""Build the legacy PostgreSQL databases from the DuckDB raw layer (docs/V3_DESIGN.md 8.1).

Two steps:

1. **Extract (DuckDB, read-only).** The latest version of every raw row is reshaped into the legacy
   base tables (``tbl_*``, ``cube_*``) and written to CSV. The legacy messiness that is not in the
   sources is a *deterministic* reshaping keyed on ids (no random draws), so a rebuild is identical:
   duplicate dealer identities, API test contracts, VIN text noise, a sentinel conversion date, and
   duplicated transaction rows.
2. **Load (Postgres).** Base tables are bulk loaded with ``COPY`` into a fresh ``<db>__build``
   database, ``legacy/ddl/*.sql`` builds the views over views, the reporting facts, the tenant mirror
   and the clutter, then the build database replaces the live one (readers never see half a build).

No primary key, foreign key or comment is ever created. Indexes exist only on contract-id columns.
"""

from __future__ import annotations

import json
import logging
import string
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import duckdb

from generator import catalog as cat
from generator import custom_fields as cf
from generator import legacy as lg
from generator.reference import LEGACY_TENANT
from legacy import ADMIN_SCHEMA, DDL_DIR, SCHEMAS, STATE_DIR, TENANT_B, databases

log = logging.getLogger("legacy.build")

# ---------------------------------------------------------------------- deterministic legacy messiness
DUP_DEALER = "(d.dealer_id * 7919) % 20 = 3 and d.is_demo = 0"  # about 5% of dealers get variant ids
INACTIVE_OFFSET, PREFIXED_OFFSET, NUMBER_PREFIX = 100_000, 200_000, "V"
LEGACY_CLAIM_OFFSET, LEGACY_CANCEL_OFFSET, LEGACY_TXN_OFFSET = 5_000_000, 5_000_000, 10_000_000
SENTINEL_DATE = "1900-01-01"
TXN_TYPES = {"SALE": 1, "CANCEL": 2, "REINSTATE": 3, "AMEND": 4, "CASH": 5}
DISB_TYPES = {"CLAIMS_RESERVE": 1, "ADMIN_FEE": 2, "COMMISSION": 3, "INSURER_FEE": 4, "PREMIUM_TAX": 5,
              "PROFIT_SHARE_RESERVE": 6}
STALE_PRODUCT_LAST_PLAN_YEAR = 2025  # the product dim stopped refreshing: later plan years are missing
TENANT_B_LAG_DAYS = 30  # the mirror schema was copied this many days before the as-of date
GEN2_LAG_DAYS = 45  # the gen2 finance copy stopped loading this many days before the as-of date
FROZEN_LAG_DAYS = 180  # report.earned_by_month_frozen cutoff

PAID_LINES = "('PAYMENT', 'PAYMENT_2', 'SUPPLEMENT')"


def _case(col: str, mapping: dict[str, int]) -> str:
    return "case " + " ".join(f"when {col} = '{k}' then {v}" for k, v in mapping.items()) + " end"


def _cur(schema: str, table: str, key: str, order: str = "_source_version desc") -> str:
    """Latest, not-deleted version of each raw row (the same rule staging uses)."""
    return (f"(select * exclude (_rn) from (select *, row_number() over (partition by {key} order by {order}) as _rn "
            f"from wh.{schema}.{table}) where _rn = 1 and not coalesce(_is_deleted, false))")


def core(table: str, key: str) -> str:
    return _cur("raw_admin_core", table, key)


def old(table: str, key: str) -> str:
    return _cur("raw_legacy_admin", table, key, "updated_at desc nulls last, _loaded_at desc")


def dealer_variant(dealer_col: str, row_id: str) -> str:
    """Contracts of a duplicated dealer land on its inactive copy (30%) or prefixed variant (20%)."""
    return (f"case when dd.dealer_id is null then {dealer_col} "
            f"when {row_id} % 10 < 3 then {dealer_col} + {INACTIVE_OFFSET} "
            f"when {row_id} % 10 < 5 then {dealer_col} + {PREFIXED_OFFSET} else {dealer_col} end")


def staging_sql() -> list[tuple[str, str]]:
    """(temp table, DuckDB select) in dependency order; the temp tables feed the exports."""
    core_test = f"coalesce(try_cast(split_part(c.contract_number, '-', 2) as bigint), 0) >= {cat.TEST_RANGE_START}"
    old_test = f"l.legacy_contract_id >= {lg.TEST_ID_START}"
    vin_noise = ("case when (l.legacy_contract_id * 31) % 100 < 6 then lower(l.vin) "
                 "when (l.legacy_contract_id * 31) % 100 < 10 then l.vin || '  ' "
                 "when (l.legacy_contract_id * 31) % 100 < 13 then ' ' || l.vin else l.vin end")
    return [
        ("s_dup", f"select d.dealer_id from {core('dealer', 'dealer_id')} d where {DUP_DEALER}"),
        ("s_product", f"""
            select p.product_id, p.product_code, p.product_name, p.plan_year, s.subcategory_code, s.subcategory_name,
                   g.category_code, g.category_name
            from {core('product', 'product_id')} p
            join {core('product_subcategory', 'subcategory_id')} s using (subcategory_id)
            join {core('product_category', 'category_id')} g on g.category_id = s.category_id"""),
        ("t_dealer", f"""
            select d.dealer_id as "DealerId", d.dealer_number as "Number", d.dealer_name as "Dealer Name",
                   d.city as "PhysicalCity", d.state as "PhysicalState", d.status as "Status",
                   d.tenant_id as "fxAdminCompanyId", d.agent_id as "fxAgentId", d.onboarded_date as "OnboardedDate"
            from {core('dealer', 'dealer_id')} d
            union all  -- the inactive copy: same number, shouted name, never cleaned up
            select d.dealer_id + {INACTIVE_OFFSET}, d.dealer_number, upper(d.dealer_name), d.city, d.state,
                   'INACTIVE', d.tenant_id, d.agent_id, d.onboarded_date - 400
            from {core('dealer', 'dealer_id')} d where {DUP_DEALER}
            union all  -- the prefixed variant created by a later re-onboarding
            select d.dealer_id + {PREFIXED_OFFSET}, '{NUMBER_PREFIX}' || d.dealer_number, d.dealer_name || '  ',
                   d.city, d.state, d.status, d.tenant_id, d.agent_id, d.onboarded_date + 200
            from {core('dealer', 'dealer_id')} d where {DUP_DEALER}"""),
        ("t_contract", f"""
            select c.contract_id as "ContractId", c.contract_number as "ContractNumber",
                   {dealer_variant('c.dealer_id', 'c.contract_id')} as "fxDealerId",
                   pl.product_id as "fxProductId", c.agent_id as "fxAgentId", c.insurer_id as "fxInsurerId",
                   c.tenant_id as "fxAdminCompanyId", c.current_obligor_id as "fxProviderId",
                   c.entered_date as "EnteredDate", c.effective_date as "EffectiveDate",
                   c.expiration_date as "ExpirationDate", c.status as "CurrentContractStatus",
                   c.term_months as "TermMonths", c.term_miles as "TermMiles",
                   c.purchase_price::decimal(12, 2) as "ContractPurchasePrice",
                   (case when {core_test} then 1 else c.msrp end)::decimal(12, 2) as "VehiclePurchasePrice",
                   c.vin as "VIN", c.vehicle_year as "VehicleYear", c.vehicle_make as "VehicleMake",
                   c.vehicle_model as "VehicleModel", c.new_used as "NewUsed",
                   c.reportable_state as "Reportable State", p.category_code as "ProductType",
                   p.subcategory_name as "ProductSubType", c.class_code as "ClassCode",
                   case when {core_test} then 'TEST' else c.customer_first_name end as "FirstName",
                   case when {core_test} then 'CUSTOMER' else c.customer_last_name end as "LastName",
                   case when {core_test} then 'TEST CUSTOMER'
                        else c.customer_first_name || ' ' || c.customer_last_name end as "FullName",
                   c.customer_email as "Email", c.customer_state as "PhysicalState",
                   c.customer_zip as "PhysicalZipCode",
                   case when {core_test} then 'svc_api' else 'svc_network' end as "EnteredByUser",
                   'CORE' as "SourceSystem", null::date as "ConversionDate"
            from {core('contract', 'contract_id')} c
            left join {core('plan', 'plan_id')} pl using (plan_id)
            left join s_product p on p.product_id = pl.product_id
            left join s_dup dd on dd.dealer_id = c.dealer_id
            union all
            select l.legacy_contract_id, l.contract_no, {dealer_variant('d.dealer_id', 'l.legacy_contract_id')},
                   p.product_id, null, null, {LEGACY_TENANT}, null, l.sale_date, l.effective_date, l.expiration_date,
                   case l.status when 'active' then 'INFORCE' when 'cancelled' then 'CANCELLED'
                                 when 'expired' then 'EXPIRED' else upper(l.status) end,
                   l.term_months, l.term_miles, l.retail_price::decimal(12, 2),
                   (case when {old_test} then 1 end)::decimal(12, 2), {vin_noise}, l.vehicle_year, l.vehicle_make,
                   l.vehicle_model, upper(l.new_used), l.customer_state, p.category_code, p.subcategory_name, null,
                   case when {old_test} then 'TEST' else l.customer_first_name end,
                   case when {old_test} then 'CUSTOMER' else l.customer_last_name end,
                   case when {old_test} then 'TEST CUSTOMER'
                        else l.customer_first_name || ' ' || l.customer_last_name end,
                   l.customer_email, l.customer_state, l.customer_zip,
                   case when {old_test} then 'svc_api' else 'conversion' end, 'CONVERSION',
                   case when (l.legacy_contract_id * 37) % 100 = 0 then date '{SENTINEL_DATE}'
                        else date '{cat.CUTOVER}' end
            from {old('contracts', 'legacy_contract_id')} l
            left join {core('dealer', 'dealer_id')} d
                   on d.dealer_number = l.dealer_code and d.tenant_id = {LEGACY_TENANT}
            left join s_product p on p.product_code = l.product_code and p.plan_year = l.plan_year
            left join s_dup dd on dd.dealer_id = d.dealer_id"""),
        ("t_product", f"""
            select product_id as "ProductId", product_code as "ProductCode", product_name as "Product",
                   category_code as "ProductType", subcategory_name as "ProductSubType", category_name as "Category",
                   plan_year as "PlanYear"
            from s_product where plan_year <= {STALE_PRODUCT_LAST_PLAN_YEAR}"""),
        ("t_product_full", """
            select product_id as "ProductKey", product_code as "Product Code", product_name as "Product",
                   category_code as "Product Type", plan_year as "Plan Year" from s_product"""),
        ("t_provider", f"""
            select obligor_id as "ProviderId", 'PRV' || lpad(obligor_id::varchar, 2, '0') as "ProviderCode",
                   obligor_name as "ProviderName" from {core('obligor', 'obligor_id')}"""),
        ("t_claim", f"""
            select c.claim_id as "ClaimId", c.claim_number as "ClaimNumber",
                   coalesce(c.contract_id, c.legacy_contract_ref) as "fxContractId", c.loss_date as "LossDate",
                   c.reported_at::date as "ReportedDate", p.paid_date as "PaidDate", c.status as "ClaimStatus",
                   c.substatus as "ClaimSubstatus", c.requested_amount::decimal(12, 2) as "RequestedAmount",
                   coalesce(p.paid, 0)::decimal(12, 2) as "PaidAmount"
            from {core('claim', 'claim_id')} c
            left join (select claim_id, sum(paid_amount) as paid,
                              max(posted_at::date) filter (where paid_amount > 0) as paid_date
                       from {core('claim_payment_line', 'line_id')}
                       where line_type in {PAID_LINES} group by 1) p using (claim_id)
            union all  -- converted claims keep the old system's sign: paid amounts are negative
            select {LEGACY_CLAIM_OFFSET} + row_number() over (order by l.claim_no), l.claim_no, l.legacy_contract_id,
                   l.loss_date, l.reported_date, l.paid_date, upper(l.status), null,
                   l.amount_requested::decimal(12, 2), coalesce(l.amount_paid, 0)::decimal(12, 2)
            from {old('claims', 'claim_no')} l"""),
        ("t_cancel", f"""
            select x.cancel_id as "CancelId", x.contract_id as "fxContractId", x.cancel_date as "CancelDate",
                   x.reason as "CancelReason", x.method as "CancelMethod",
                   x.customer_refund::decimal(12, 2) as "CustomerRefund", x.cancel_fee::decimal(12, 2) as "CancelFee"
            from {core('cancellation', 'cancel_id')} x
            union all
            select {LEGACY_CANCEL_OFFSET} + try_cast(regexp_extract(l.cancellation_id, '[0-9]+') as bigint),
                   l.legacy_contract_id, l.cancel_date, l.cancel_reason, upper(l.cancel_method),
                   l.refund_amount::decimal(12, 2), l.cancel_fee::decimal(12, 2)
            from {old('cancellations', 'cancellation_id')} l"""),
        ("t_stock_number", """
            -- the dealer stock list: VIN typed by hand at the dealer, no contract id to join on
            select 'STK' || lpad(c."ContractId"::varchar, 9, '0') as stock_number,
                   case when (c."ContractId" * 13) % 100 < 9 then lower(trim(c."VIN"))
                        when (c."ContractId" * 13) % 100 < 16 then trim(c."VIN") || ' '
                        when (c."ContractId" * 13) % 100 < 20 then '  ' || trim(c."VIN")
                        else trim(c."VIN") end as "VIN",
                   d."Number" as "DealerNumber", c."EnteredDate" as "ReceivedDate"
            from t_contract c join t_dealer d on d."DealerId" = c."fxDealerId"
            where (c."ContractId" * 17) % 10 < 7 and c."VIN" is not null"""),
        ("t_rep", f"""
            select row_number() over (order by d.dealer_id) as "RepId", a.agent_name as "Reps Name",
                   d.dealer_number as "Number", regexp_replace(d.dealer_number, '[^0-9]', '', 'g') as "Dealer Number"
            from {core('dealer', 'dealer_id')} d join {core('agent', 'agent_id')} a on a.agent_id = d.agent_id"""),
        ("t_demo_dealers", f"""
            select d.dealer_id as "Dealer Id", d.dealer_name as "Dealer Name"
            from {core('dealer', 'dealer_id')} d where d.is_demo = 1"""),
        ("t_transaction", f"""
            select t.txn_id as "TransactionId", t.contract_id as "fxContractId",
                   {_case('t.txn_type', TXN_TYPES)} as "fxTransactionTypeId", t.txn_id * 10 as "fxCashTransactionId",
                   t.posted_at::date as "PostedDate", t.amount::decimal(14, 2) as "Amount"
            from {core('transaction', 'txn_id')} t
            union all  -- re-sent rows: same transaction id twice; consumers must dedupe (max cash id)
            select t.txn_id, t.contract_id, {_case('t.txn_type', TXN_TYPES)}, t.txn_id * 10 + 1,
                   t.posted_at::date, t.amount::decimal(14, 2)
            from {core('transaction', 'txn_id')} t where t.txn_id % 40 = 7
            union all
            select {LEGACY_TXN_OFFSET} + c."ContractId", c."ContractId", 1, ({LEGACY_TXN_OFFSET} + c."ContractId") * 10,
                   c."EnteredDate", c."ContractPurchasePrice"::decimal(14, 2)
            from t_contract c where c."SourceSystem" = 'CONVERSION'"""),
        ("t_disbursement", f"""
            select d.disbursement_id as "DisbursementId", d.txn_id as "fxTransactionId",
                   d.contract_id as "fxContractId", {_case('t.txn_type', TXN_TYPES)} as "fxTransactionTypeId",
                   {_case('d.disbursement_type', DISB_TYPES)} as "fxDisbursementTypeId",
                   d.account_holder_id as "fxAccountHolderId", d.amount::decimal(14, 2) as "Amount",
                   d.posted_at::date as "PostedDate"
            from {core('disbursement', 'disbursement_id')} d
            left join {core('transaction', 'txn_id')} t using (txn_id)
            union all
            select {LEGACY_TXN_OFFSET} * 10 + l.legacy_contract_id * 10 + k.n,
                   {LEGACY_TXN_OFFSET} + l.legacy_contract_id, l.legacy_contract_id, 1, k.n, null,
                   (case k.n when 1 then l.reserve_amount when 2 then l.admin_fee
                         else l.commission end)::decimal(14, 2),
                   l.sale_date
            from {old('contracts', 'legacy_contract_id')} l cross join (select unnest([1, 2, 3]) as n) k"""),
        ("t_cession_type", f"""
            select row_number() over (order by structure_type) as "CessionTypeId", structure_type as "CessionType"
            from (select distinct structure_type from {core('participation_entity', 'entity_id')})"""),
    ]


EXPORTS: list[tuple[str, str, str]] = [  # (schema, legacy table, temp table)
    ("legacy_ops", "tbl_contract", "t_contract"),
    ("legacy_ops", "tbl_dealer", "t_dealer"),
    ("legacy_ops", "tbl_product", "t_product"),
    ("legacy_ops", "tbl_provider", "t_provider"),
    ("legacy_ops", "tbl_claim", "t_claim"),
    ("legacy_ops", "tbl_cancel", "t_cancel"),
    ("legacy_ops", "tbl_stock_number", "t_stock_number"),
    ("legacy_ops", "tbl_rep", "t_rep"),
    ("legacy_ops", "dim_demo_dealers", "t_demo_dealers"),
    ("legacy_finance", "cube_transaction", "t_transaction"),
    ("legacy_finance", "cube_disbursement", "t_disbursement"),
    ("legacy_finance", "tbl_cession_type", "t_cession_type"),
    ("legacy_reporting", "DimProduct", "t_product_full"),
]
ADMIN_EXPORTS: list[tuple[str, str, str]] = [
    (ADMIN_SCHEMA, "field_definition", "t_field_definition"),
    (ADMIN_SCHEMA, "contract_field", "t_contract_field"),
]
DDL_FILES = ("10_legacy_ops.sql", "20_legacy_finance.sql", "30_legacy_reporting.sql", "40_tenant_b.sql",
             "50_clutter.sql", "60_indexes.sql")


def _eav(con: duckdb.DuckDBPyConnection) -> None:
    """The admin system's custom fields: raw EAV rows when ingested, else rebuilt from raw contracts with
    the generator's own (pure) function, so an older warehouse still builds the same store."""
    have = con.execute("select count(*) from duckdb_tables() where database_name = 'wh' and "
                       "schema_name = 'raw_admin_core' and table_name = 'contract_custom_field'").fetchone()[0]
    if have:
        con.execute(f"""create temp table t_contract_field as
            select custom_field_id as "ContractFieldId", contract_id as "fxContractId", field_id as "fxFieldId",
                   value_text as "Value" from {core('contract_custom_field', 'custom_field_id')}""")
        con.execute(f"""create temp table t_field_definition as
            select distinct field_id as "FieldId", field_name as "FieldName", data_type as "DataType"
            from {core('custom_field_def', 'field_def_id')}""")
        return
    contracts = con.execute(f"select * from {core('contract', 'contract_id')}").df()
    eav = cf.contract_values(contracts)  # noqa: F841 - read by the DuckDB replacement scan below
    defs = cf.field_defs([1], "2018-12-01 09:00:00")  # noqa: F841
    con.execute("""create temp table t_contract_field as select custom_field_id as "ContractFieldId",
                   contract_id as "fxContractId", field_id as "fxFieldId", value_text as "Value" from eav""")
    con.execute("""create temp table t_field_definition as select field_id as "FieldId", field_name as "FieldName",
                   data_type as "DataType" from defs""")


def as_of_date(con: duckdb.DuckDBPyConnection) -> date:
    return con.execute(f"select max(entered_date) from {core('contract', 'contract_id')}").fetchone()[0]


def extract(warehouse: Path, out: Path) -> tuple[dict, date]:
    """Reshape raw rows into the legacy base tables; one CSV per table. Returns ({fq: (csv, columns)}, as_of)."""
    if not warehouse.exists():
        raise FileNotFoundError(f"{warehouse} not found: run `python -m pipeline.run` first")
    con = duckdb.connect()
    con.execute(f"attach '{warehouse.as_posix()}' as wh (read_only)")
    try:
        for name, sql in staging_sql():
            con.execute(f"create temp table {name} as {sql}")
        _eav(con)
        files: dict[str, tuple[Path, list[tuple[str, str]]]] = {}
        for schema, table, temp in EXPORTS + ADMIN_EXPORTS:
            path = out / f"{schema}.{table}.csv"
            cols = [(r[0], r[1]) for r in con.execute(f"describe {temp}").fetchall()]
            con.execute(f"copy {temp} to '{path.as_posix()}' (format csv, header false, nullstr '\\N')")
            files[f"{schema}.{table}"] = (path, cols)
        return files, as_of_date(con)
    finally:
        con.close()


# ---------------------------------------------------------------------- Postgres
def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def pg_type(duck_type: str) -> str:
    from warehouse.postgres.sync import TYPE_MAP

    t = duck_type.upper()
    if t.startswith("DECIMAL"):
        return t.replace("DECIMAL", "numeric").lower()
    return TYPE_MAP.get(t, "text")


def load(con, files: dict, names: list[str]) -> dict[str, int]:
    """CREATE TABLE (no constraints) + COPY for each base table; returns row counts."""
    counts = {}
    with con.cursor() as cur:
        for fq in names:
            path, cols = files[fq]
            schema, table = fq.split(".", 1)
            col_sql = ", ".join(f"{_q(c)} {pg_type(t)}" for c, t in cols)
            cur.execute(f"create table {_q(schema)}.{_q(table)} ({col_sql})")
            with path.open("rb") as fh, cur.copy(
                    f"copy {_q(schema)}.{_q(table)} from stdin (format csv, null '\\N')") as cp:
                while chunk := fh.read(1 << 20):
                    cp.write(chunk)
            counts[fq] = cur.execute(f"select count(*) from {_q(schema)}.{_q(table)}").fetchone()[0]
    return counts


def render(name: str, params: dict[str, object]) -> str:
    return string.Template((DDL_DIR / name).read_text(encoding="utf-8")).substitute(params)


def ddl_params(as_of: date) -> dict[str, object]:
    def iso(d: date) -> str:
        return d.isoformat()

    bak = as_of - timedelta(days=400)
    return {"as_of": iso(as_of), "cutover": iso(cat.CUTOVER), "tenant_b": TENANT_B,
            "tenant_b_cutoff": iso(as_of - timedelta(days=TENANT_B_LAG_DAYS)),
            "gen2_cutoff": iso(as_of - timedelta(days=GEN2_LAG_DAYS)),
            "frozen_cutoff_id": int((as_of - timedelta(days=FROZEN_LAG_DAYS)).strftime("%Y%m%d")),
            "bak_date": bak.strftime("%Y%m%d"), "bak_iso": iso(bak),
            "bak_date2": (as_of - timedelta(days=95)).strftime("%Y%m%d")}


def _recreate(admin_con, name: str) -> None:
    admin_con.execute(f"drop database if exists {_q(name)} with (force)")
    admin_con.execute(f"create database {_q(name)}")


def _grant(con, schemas: list[str], reader: str, locked: tuple[str, ...] = ()) -> bool:
    if not con.execute("select 1 from pg_roles where rolname = %s", (reader,)).fetchone():
        return False
    for s in schemas:
        if s in locked:  # personal staging: nobody else can read it (and so nobody can build on it)
            con.execute(f"revoke all on schema {_q(s)} from public, {_q(reader)}")
            continue
        con.execute(f"grant usage on schema {_q(s)} to {_q(reader)}")
        con.execute(f"grant select on all tables in schema {_q(s)} to {_q(reader)}")
    return True


def sandbox(con, as_of: date) -> list[str]:
    """zz_sandbox_analyst: one prebuilt compliance summary per entered year (someone's personal staging)."""
    made = []
    for y in range(cat.CUTOVER.year, as_of.year + 1):
        t = f"compliance_summary_{y}"
        con.execute(f"""create table zz_sandbox_analyst.{t} as
            select "Reportable State", "ProductType", count(*) as contracts, sum("ContractPurchasePrice") as written
            from legacy_ops.vw_dim_contracts where extract(year from "EnteredDate") = {y} group by 1, 2""")
        made.append(t)
    return made


def build(warehouse: Path | None = None, settings=None, database: str | None = None,
          admin_database: str | None = None) -> dict:
    """Extract, load and swap in both legacy databases. Returns the build manifest (also written to disk)."""
    from warehouse.postgres.sync import WAREHOUSE_PATH, Settings, available

    s = settings or Settings.from_env()
    if (why := available(s)) is not None:
        raise RuntimeError(f"PostgreSQL is not available: {why}")
    main_db, admin_db = databases()
    main_db, admin_db = database or main_db, admin_database or admin_db
    warehouse = warehouse or WAREHOUSE_PATH
    t0 = time.perf_counter()
    timings: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="legacy_") as tmp:
        files, as_of = extract(Path(warehouse), Path(tmp))
        timings["extract_s"] = round(time.perf_counter() - t0, 1)
        params = ddl_params(as_of)
        staging = {main_db: f"{main_db}__build", admin_db: f"{admin_db}__build"}
        with s.connect(database="postgres", autocommit=True) as pc:
            for b in staging.values():
                _recreate(pc, b)
        counts: dict[str, int] = {}
        t1 = time.perf_counter()
        with s.connect(database=staging[main_db]) as con:
            for sch in SCHEMAS:
                con.execute(f"create schema {_q(sch)}")
            counts |= load(con, files, [f"{a}.{b}" for a, b, _ in EXPORTS])
            timings["load_s"] = round(time.perf_counter() - t1, 1)
            for f in DDL_FILES:
                t2 = time.perf_counter()
                con.execute(render(f, params))
                timings[f.removesuffix(".sql") + "_s"] = round(time.perf_counter() - t2, 1)
            sandbox(con, as_of)
            con.commit()
        with s.connect(database=staging[admin_db]) as con:
            con.execute(f"create schema {_q(ADMIN_SCHEMA)}")
            counts |= load(con, files, [f"{a}.{b}" for a, b, _ in ADMIN_EXPORTS])
            con.commit()
    t3 = time.perf_counter()
    with s.connect(database=staging[main_db], autocommit=True) as con:
        con.execute("analyze")
        granted = _grant(con, list(SCHEMAS), s.reader, locked=("zz_sandbox_analyst",))
        counts |= table_counts(con)
    with s.connect(database=staging[admin_db], autocommit=True) as con:
        con.execute("analyze")
        _grant(con, [ADMIN_SCHEMA], s.reader)
    timings["analyze_s"] = round(time.perf_counter() - t3, 1)
    with s.connect(database="postgres", autocommit=True) as pc:  # swap: the live db is replaced in one step
        for final, b in staging.items():
            pc.execute(f"drop database if exists {_q(final)} with (force)")
            pc.execute(f"alter database {_q(b)} rename to {_q(final)}")
    timings["total_s"] = round(time.perf_counter() - t0, 1)
    manifest = {"database": main_db, "admin_database": admin_db, "as_of": as_of.isoformat(),
                "warehouse": str(warehouse), "reader_granted": granted, "params": params,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "timings": timings, "row_counts": counts}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"build_{main_db}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    log.info("built %s + %s in %.1fs", main_db, admin_db, timings["total_s"])
    return manifest


def table_counts(con) -> dict[str, int]:
    """Exact row counts for every base table in the legacy schemas (views are not counted)."""
    rows = con.execute("""select n.nspname, c.relname from pg_catalog.pg_class c
                          join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                          where c.relkind = 'r' and n.nspname = any(%s) order by 1, 2""", (list(SCHEMAS),)).fetchall()
    return {f"{s}.{t}": con.execute(f"select count(*) from {_q(s)}.{_q(t)}").fetchone()[0] for s, t in rows}


def exists(settings=None, database: str | None = None) -> bool:
    from warehouse.postgres.sync import Settings

    s = settings or Settings.from_env()
    name = database or databases()[0]
    with s.connect(database="postgres", autocommit=True, timeout=5) as pc:
        return pc.execute("select 1 from pg_database where datname = %s", (name,)).fetchone() is not None


def drop(settings=None) -> list[str]:
    from warehouse.postgres.sync import Settings

    s = settings or Settings.from_env()
    gone = []
    with s.connect(database="postgres", autocommit=True) as pc:
        for name in databases():
            for n in (name, f"{name}__build"):
                if pc.execute("select 1 from pg_database where datname = %s", (n,)).fetchone():
                    pc.execute(f"drop database {_q(n)} with (force)")
                    gone.append(n)
    return gone
