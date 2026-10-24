-- pattern:  loss ratio from the earnings series (modern star schema)
-- question: The same monthly loss ratio since Jan 2025 from the modern earned-disbursement fact and the
--           claim fact.
-- trap:     none expected. Every earned row has a real earned_month_key (the modern fact has no sentinel
--           date id), and paid_amount is already positive. The ratio still uses the
--           claims-reserve disbursement only, the same basis as the legacy script.

set statement_timeout = '5min';

with e as (
    select earned_month_key as month_id,
           sum(earned_amount) as earned
    from core.fct_earned_disbursement
    where disbursement_type = 'CLAIMS_RESERVE'
        and earned_month_key between 202501 and 202610
    group by 1
), p as (
    select to_char(paid_date, 'YYYYMM')::int as month_id,
           sum(paid_amount) as paid
    from gold.fct_claim
    where paid_date between date '2025-01-01' and date '2026-10-31'
    group by 1
)
select month_id,
       e.earned,
       p.paid,
       round((100 * p.paid / nullif(e.earned, 0))::numeric, 1) as loss_ratio_pct
from e
full join p using (month_id)
order by 1;

set statement_timeout = '60s';
