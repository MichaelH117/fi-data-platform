-- Remittance aging per dealer x posting period x age band (entered -> received), with late fees.
-- Built on fct_cash_transaction (remit_age_days, is_late and late_fee come from the core fact; the
-- late rule lives in dbt vars remit_late_days / remit_late_fee_pct). Bands: seed remit_aging_buckets,
-- the ONE remittance aging-band definition, shared with finance.mart_remittance_late_fees (the two
-- marts tie out in dbt/tests/dealer/assert_remittance_late_fees_agree_with_finance.sql).
select
    f.tenant_key,
    f.dealer_key,
    d.dealer_number,
    d.dealer_name,
    f.posting_period_key,
    b.sort_order as age_band_order,
    b.aging_bucket as age_band,
    count(*) as cash_transactions,
    count(*) filter (where f.is_late) as late_transactions,
    round(sum(f.amount), 2) as remitted_amount,
    round(sum(f.late_fee), 2) as late_fees,
    round(avg(f.remit_age_days), 1) as avg_remit_age_days,
    max(f.remit_age_days) as max_remit_age_days,
    {{ audit_columns() }}
from {{ ref('fct_cash_transaction') }} as f
left join {{ ref('dim_dealer') }} as d on d.dealer_key = f.dealer_key
left join {{ ref('remit_aging_buckets') }} as b
    on greatest(coalesce(f.remit_age_days, 0), 0) between b.min_days and b.max_days
group by all
