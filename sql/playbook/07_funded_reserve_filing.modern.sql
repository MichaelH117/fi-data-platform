-- pattern:  funded-reserve filing pull (modern star schema)
-- question: The same filing lines for the same provider entity, read from the regulatory marts. They
--           already carry each state's reporting window (period_start / period_end), the scope flag,
--           the written / in-force-at-period-end flags, and positive coverage cost and paid amounts.
-- trap:     none expected. The window logic, scope and sign fix live in one tested model, so the analyst
--           only filters and sums. The provider is picked by its id in the obligor dim, not by a name.
--           Note the modern scope is wider (it includes the specialty line) and states without a filing
--           requirement carry no window, so the totals differ from the legacy pull by design.

set statement_timeout = '60s';

with o as (
    select obligor_key
    from core.dim_obligor
    where obligor_id = 2
), k as (
    select k.state,
           k.period_end,
           count(*) filter (where k.is_written_itd) as q1_written_itd,
           count(*) filter (where k.is_written_in_period) as q1_written_period,
           count(*) filter (where k.status = 'INFORCE') as q2_in_force_by_status,
           count(*) filter (where k.is_in_force_at_period_end) as q2_in_force_at_period_end,
           sum(k.coverage_cost) filter (where k.is_in_force_at_period_end) as q3_gross_consideration_cost,
           sum(k.retail_price) filter (where k.is_in_force_at_period_end) as q3_gross_consideration_retail
    from regulatory.mart_funded_reserve_contracts k
    join o on o.obligor_key = k.provider_key
    where k.is_filing_product_scope
        and k.has_requirement
    group by 1, 2
), c as (
    select m.state,
           sum(m.paid_itd) as q4_claims_paid_itd,
           sum(m.paid_in_period) as q4_claims_paid_period
    from regulatory.mart_funded_reserve_claims m
    join o on o.obligor_key = m.provider_key
    where m.is_filing_product_scope
        and m.has_requirement
    group by 1
)
select k.*,
       c.q4_claims_paid_itd,
       c.q4_claims_paid_period,
       k.q3_gross_consideration_cost - coalesce(c.q4_claims_paid_itd, 0) as q5_net_itd,
       round((0.40 * (k.q3_gross_consideration_cost - coalesce(c.q4_claims_paid_itd, 0)))::numeric, 2) as q6_reserve_40pct
from k
left join c using (state)
order by 1;
