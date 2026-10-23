-- Grain: contract x posting period of sale or amendment (gross written).
-- coverage_cost = the SALE / AMEND transaction amounts (dealer-cost basis); purchase_price and
-- retail_price are booked in the sale period only. refund_pct is the effective cancellation's.
with written as (
    select contract_key, posting_period_key,
        sum(amount) filter (where txn_type = 'SALE') as sale_coverage_cost,
        sum(amount) filter (where txn_type = 'AMEND') as amend_coverage_cost,
        min(posted_at) as first_posted_at,
        bool_or(txn_type = 'SALE') as has_sale
    from {{ ref('int_transactions_posted') }}
    where txn_type in ('SALE', 'AMEND')
    group by 1, 2
)

select
    w.contract_key,
    w.posting_period_key,
    {{ period_start_date("w.posting_period_key") }} as posting_period_start_date,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    k.entered_date_key,
    {{ date_key('w.first_posted_at') }} as posted_date_key,
    coalesce(w.sale_coverage_cost, 0) + coalesce(w.amend_coverage_cost, 0) as coverage_cost,
    coalesce(w.sale_coverage_cost, 0) as sale_coverage_cost,
    coalesce(w.amend_coverage_cost, 0) as amend_coverage_cost,
    case when w.has_sale then f.purchase_price else 0 end as purchase_price,
    case when w.has_sale then f.retail_price else 0 end as retail_price,
    case when w.has_sale then 1 else 0 end as contracts_written,
    x.refund_pct,
    {{ audit_columns() }}
from written as w
inner join {{ ref('dim_contract') }} as k using (contract_key)
left join {{ ref('dim_contract_finance') }} as f using (contract_key)
left join {{ ref('int_cancellations_conformed') }} as x
    on x.contract_key = w.contract_key and x.is_effective_cancel
