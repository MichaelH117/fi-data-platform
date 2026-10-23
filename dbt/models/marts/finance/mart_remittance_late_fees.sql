-- Remittance aging and dealer late-fee billing (finance view). Grain: dealer x posting period of
-- the cash receipt x aging bucket (seed remit_aging_buckets). remit_age_days, is_late and late_fee
-- come from fct_cash_transaction (dbt vars remit_late_days and remit_late_fee_pct), so the bill
-- ties to the cash ledger. The dealer analyst's mart_remittance_aging covers open (unremitted) items.
with buckets as (
    select * from {{ ref('remit_aging_buckets') }}
)

select
    c.dealer_key,
    d.dealer_number,
    d.dealer_name,
    c.tenant_key,
    c.posting_period_key,
    case when p.is_closed then 'FINAL' else 'PRELIMINARY' end as billing_status,
    b.aging_bucket,
    b.sort_order as aging_sort,
    count(*) as receipts,
    sum(c.amount) as remitted_amount,
    count(*) filter (where c.is_late) as late_receipts,
    coalesce(sum(c.amount) filter (where c.is_late), 0) as late_amount,
    sum(c.late_fee) as late_fee_billed,
    avg(c.remit_age_days) as avg_remit_age_days,
    max(c.remit_age_days) as max_remit_age_days,
    {{ audit_columns() }}
from {{ ref('fct_cash_transaction') }} as c
inner join buckets as b on c.remit_age_days between b.min_days and b.max_days
left join {{ ref('dim_dealer') }} as d using (dealer_key)
left join {{ ref('dim_posting_period') }} as p using (posting_period_key)
group by all
