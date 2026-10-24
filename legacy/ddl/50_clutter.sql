-- Clutter: a frozen report schema, empty shells, backups (also inside serving schemas). Nobody owns any of it.

create table report.earned_by_month_frozen as
select e."fxEarningDateId" / 100 as "EarningMonthId", sum(e."EarnedAmount") as "EarnedAmount", count(*) as "Rows"
from legacy_finance.cube_earned_disbursement e
where e."fxEarningDateId" <= ${frozen_cutoff_id}
group by 1;

create table report.tbl_contract_new (like legacy_ops.tbl_contract);
create table report.tbl_claim_new (like legacy_ops.tbl_claim);
create table report.tbl_dealer_bak_${bak_date} as select * from legacy_ops.tbl_dealer;
create table report.tbl_contract_bak_${bak_date} as
select * from legacy_ops.tbl_contract where "EnteredDate" <= date '${bak_iso}';

-- inside the serving schemas
create table legacy_ops.tbl_dealer_bak_${bak_date2} as select * from legacy_ops.tbl_dealer;
create table legacy_finance.cube_disbursement_old as
select * from legacy_finance.cube_disbursement where "fxContractId" % 10 = 0;
create table legacy_finance.tbl_fact_gross_premiums_new (like legacy_finance.tbl_fact_gross_premiums);
create table legacy_reporting."FactClaims_tmp" (like legacy_reporting."FactClaims");
