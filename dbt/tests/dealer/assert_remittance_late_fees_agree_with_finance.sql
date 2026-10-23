-- One aging-band definition (seed remit_aging_buckets) feeds both remittance marts, so the dealer
-- view and the finance late-fee bill must agree per dealer x posting period x band.
with dealer as (
    select dealer_key, posting_period_key, age_band, sum(cash_transactions) as n, sum(late_fees) as fees
    from {{ ref('mart_remittance_aging') }} group by all
),

fin as (
    select dealer_key, posting_period_key, aging_bucket as age_band, sum(receipts) as n,
        sum(late_fee_billed) as fees
    from {{ ref('mart_remittance_late_fees') }} group by all
)

select coalesce(d.dealer_key, f.dealer_key) as dealer_key,
    coalesce(d.posting_period_key, f.posting_period_key) as posting_period_key,
    coalesce(d.age_band, f.age_band) as age_band, d.n, f.n as fin_n, d.fees, f.fees as fin_fees
from dealer as d
full outer join fin as f using (dealer_key, posting_period_key, age_band)
where coalesce(d.n, 0) <> coalesce(f.n, 0) or abs(coalesce(d.fees, 0) - coalesce(f.fees, 0)) > 0.01
