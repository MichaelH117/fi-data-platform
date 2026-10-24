-- database: legacy
-- timeout: 60s
-- question: Paid claims by month (total over the last 24 months shown as the answer).
-- trap:     the ops layer stores the claim amount NEGATIVE and the reporting layer POSITIVE. A report that
--           appends both layers (ops for the status detail, reporting for the producer split) nets to ~0.
set statement_timeout = '60s';

with ops as (
    select date_trunc('month', "PaidDate")::date as m, sum("ClaimAmount") as amt
    from legacy_ops.vw_dim_claims
    where "PaidDate" >= date '2024-11-01'
    group by 1
), rpt as (
    select date_trunc('month', cl."PaidDate")::date as m, sum(f."Claim Amount") as amt
    from legacy_reporting.vw_fact_claims f
    join legacy_ops.tbl_claim cl on cl."ClaimNumber" = f."Claim Number" and cl."fxContractId" = f."fxContractId"
    where cl."PaidDate" >= date '2024-11-01'
    group by 1
)
select round(sum(amt), 2) as paid_claims_appended_layers,
       round(sum(amt) filter (where src = 'ops'), 2) as ops_layer,
       round(sum(amt) filter (where src = 'rpt'), 2) as reporting_layer,
       count(distinct m) as months
from (select 'ops' as src, * from ops union all select 'rpt', * from rpt) u;
