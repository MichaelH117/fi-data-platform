-- database: modern
-- timeout: 120s
-- question: Loss ratio for entered year 2019 from the modern earned-disbursement fact (claims reserve) and
--           the claim fact.
-- fix:      the earnings fact carries a real month on every row (the sentinel date is mapped to NULL plus a
--           flag in staging), and Postgres has a BRIN index on month_start, so the range does not full-scan.
set statement_timeout = '120s';

with e as (
    select sum(e.earned_amount) as earned
    from core.fct_earned_disbursement e
    join core.dim_contract c on c.contract_key = e.contract_key
    where e.month_start between date '2019-01-01' and date '2026-10-31'
      and e.disbursement_type = 'CLAIMS_RESERVE' and c.entered_year = 2019
), p as (
    select sum(f.paid_amount) as paid
    from gold.fct_claim f
    join core.dim_contract c on c.contract_key = f.contract_key
    where f.paid_date is not null and c.entered_year = 2019
)
select round((100 * p.paid / nullif(e.earned, 0))::numeric, 2) as loss_ratio_pct, round(e.earned::numeric, 2) as earned,
       round(p.paid::numeric, 2) as paid
from e, p;
