-- Grain: contract x disbursement type x posting period x earned month.
-- earned_month_key is the month being earned; posting_period_key is the period it is booked in.
-- They differ only for catch-up earning (a month already closed when the contract activated is
-- booked in the first open period) and VOID reversals, so closed periods are never restated
-- (docs/V3_DESIGN.md 2.5, int_contract_months).
-- earned_pct is the share of the bucket earned in the period (pro-rata default, rule of 78 via
-- var earning_method); earned_reserve is the CLAIMS_RESERVE bucket's earned amount.
-- cancel_true_up_amount: the retained part of a cancelled contract's bucket, earned in the cancel's
-- posting period (int_disbursements_earned), so earned + refunded = written once cancelled.
select
    e.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    e.disbursement_type,
    e.posting_period_key,
    {{ period_start_date("e.posting_period_key") }} as posting_period_start_date,
    e.month_start,
    e.earned_month_key,
    e.written_amount,
    e.earned_pct,
    e.earned_pct_cumulative,
    e.earned_amount,
    e.cancel_true_up_amount,
    e.earned_reserve
from {{ ref('int_disbursements_earned') }} as e
inner join {{ ref('dim_contract') }} as k using (contract_key)
