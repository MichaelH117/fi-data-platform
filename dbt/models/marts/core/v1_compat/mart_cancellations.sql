-- DEPRECATED V1 mart (gold.mart_cancellations), rebuilt on the V3 core facts.
-- Grain: dealer_code x sale-month cohort.
{{ config(schema='gold', materialized='table') }}
{% set reasons = ['customer_request', 'dealer_unwind', 'loan_payoff', 'dealer_error', 'repossession', 'total_loss'] %}
with sales as (
    select
        dealer_code,
        cast(date_trunc('month', sale_date) as date) as sale_month,
        count(*) as contracts_sold,
        sum(retail_price) as retail_sold
    from {{ ref('fct_contract_sale') }}
    group by all
),

cancels as (
    select
        dealer_code,
        cast(date_trunc('month', sale_date) as date) as sale_month,
        count(*) as contracts_cancelled,
        count(*) filter (where days_in_force <= 90) as cancelled_within_90_days,
        avg(days_in_force) as avg_days_to_cancel,
        sum(refund_amount) as refund_amount,
        sum(expected_refund) as expected_refund,
        sum(cancel_fee) as cancel_fees,
        sum(unearned_reserve_returned) as unearned_reserve_returned,
        sum(dealer_chargeback) as dealer_chargeback,
        {% for r in reasons %}
        count(*) filter (where cancel_reason = '{{ r }}') as cancels_{{ r }}{% if not loop.last %},{% endif %}
        {% endfor %}
    from {{ ref('v1_fct_cancellation') }}
    group by all
)

select
    d.dealer_key,
    s.dealer_code,
    d.dealer_name,
    d.state,
    s.sale_month,
    s.contracts_sold,
    coalesce(c.contracts_cancelled, 0) as contracts_cancelled,
    {{ safe_divide('coalesce(c.contracts_cancelled, 0)', 's.contracts_sold') }} as cancel_rate,
    coalesce(c.cancelled_within_90_days, 0) as cancelled_within_90_days,
    round(c.avg_days_to_cancel, 1) as avg_days_to_cancel,
    round(coalesce(c.refund_amount, 0), 2) as refund_amount,
    round(coalesce(c.expected_refund, 0), 2) as expected_refund,
    round(coalesce(c.cancel_fees, 0), 2) as cancel_fees,
    round(coalesce(c.unearned_reserve_returned, 0), 2) as unearned_reserve_returned,
    round(coalesce(c.dealer_chargeback, 0), 2) as dealer_chargeback,
    {% for r in reasons %}
    coalesce(c.cancels_{{ r }}, 0) as cancels_{{ r }}{% if not loop.last %},{% endif %}
    {% endfor %}
from sales as s
left join cancels as c using (dealer_code, sale_month)
left join {{ ref('v1_dim_dealer') }} as d on s.dealer_code = d.dealer_code and d.is_current
