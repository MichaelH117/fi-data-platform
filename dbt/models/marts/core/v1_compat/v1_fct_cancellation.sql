-- DEPRECATED V1 shape (gold.fct_cancellation): one row per cancelled contract (its effective
-- cancellation), with the V1 pro-rata expected-refund check. Use core.fct_cancellation instead.
{{ config(schema='gold', alias='fct_cancellation', materialized='table') }}
select
    x.cancel_key as cancellation_key,
    x.source_cancel_id as cancellation_id,
    s.contract_key,
    s.contract_id,
    s.dealer_key,
    s.dealer_code,
    s.product_key,
    s.product_code,
    s.sale_date,
    x.cancel_date,
    x.cancel_date_key,
    datediff('day', s.effective_date, x.cancel_date) as days_in_force,
    case x.reason
        when 'Customer Request' then 'customer_request'
        when 'Unwound Deal' then 'dealer_unwind'
        when 'Payoff/Trade' then 'loan_payoff'
        when 'Repossession' then 'repossession'
        when 'Total Loss' then 'total_loss'
        when 'Dealer Error' then 'dealer_error'
        else lower(replace(x.reason, ' ', '_'))
    end as cancel_reason,
    x.odometer as odometer_at_cancel,
    greatest(0, least(1, 1 - datediff('day', s.effective_date, x.cancel_date) / nullif(s.term_days, 0))) as unearned_pct,
    1 - greatest(0, least(1, 1 - datediff('day', s.effective_date, x.cancel_date) / nullif(s.term_days, 0))) as earned_pct,
    x.customer_refund as refund_amount,
    x.cancel_fee,
    greatest(0, round(s.retail_price * greatest(0, least(1, 1 - datediff('day', s.effective_date, x.cancel_date)
        / nullif(s.term_days, 0))) - {{ var('cancel_fee') }}, 2)) as expected_refund,
    round(x.customer_refund - greatest(0, s.retail_price * greatest(0, least(1, 1 - datediff('day', s.effective_date,
        x.cancel_date) / nullif(s.term_days, 0))) - {{ var('cancel_fee') }}), 2) as refund_variance,
    round(s.reserve_amount * greatest(0, least(1, 1 - datediff('day', s.effective_date, x.cancel_date)
        / nullif(s.term_days, 0))), 2) as unearned_reserve_returned,
    x.dealer_portion as dealer_chargeback,
    x.is_amount_sign_fixed
from {{ ref('fct_cancellation') }} as x
inner join {{ ref('fct_contract_sale') }} as s using (contract_key)
where x.is_effective_cancel
