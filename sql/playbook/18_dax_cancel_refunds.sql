-- pattern:  DAX measure -> SQL translation 4 of 4 (same name, different population and sign)
-- question: Cancel refunds by cancel month in the Ops model and in the Finance Gen3 copy, last 12 months.
-- source:   legacy/semantic_models/Ops.SemanticModel, table Cancels (legacy_ops.vw_dim_cancels)
--             'Cancel Refunds' = SUM(Cancels[CustomerRefund])
--           legacy/semantic_models/Finance.SemanticModel, table Fact Cancels Gen3 (legacy_finance.vw_fact_cancels_gen3)
--             'Cancel Refunds' = SUM('Fact Cancels Gen3'[RefundAmount])
-- translate: a date axis on CancelDate is a group by on date_trunc('month', ...); COUNTROWS of the same
--           table next to it is count(*), which exposes the rows one model silently drops.
-- trap:     Gen3 negates the refund AND drops every cancel whose refund is not positive, so its total has
--           the opposite sign and its cancel count is lower. A cancel rate built on Gen3 rows undercounts.

set statement_timeout = '60s';

with o as (
    select date_trunc('month', "CancelDate")::date as mo,
           count(*) as ops_cancels,
           sum("CustomerRefund") as ops_cancel_refunds
    from legacy_ops.vw_dim_cancels
    where "CancelDate" > date '2025-10-21'
    group by 1
), g as (
    select date_trunc('month', "CancelDate")::date as mo,
           count(*) as gen3_cancels,
           sum("RefundAmount") as gen3_cancel_refunds
    from legacy_finance.vw_fact_cancels_gen3
    where "CancelDate" > date '2025-10-21'
    group by 1
)
select mo,
       o.ops_cancels,
       g.gen3_cancels,
       o.ops_cancel_refunds,
       g.gen3_cancel_refunds,
       o.ops_cancel_refunds + g.gen3_cancel_refunds as sum_is_zero_when_only_sign_differs
from o
full join g using (mo)
order by 1;
