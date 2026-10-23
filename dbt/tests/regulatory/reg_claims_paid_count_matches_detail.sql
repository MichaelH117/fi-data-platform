-- Reconciliation: Section 2 claims paid / denied counts for the regulatory year equal the claim
-- detail tab (claims closed in the period, by status set), per state and provider.
with summary as (
    select state, provider_key, claims_paid_count, claims_denied_count
    from {{ ref('mart_funded_reserve_summary') }}
    where scope = 'PERIOD' and basis = 'COVERAGE_COST'
),

detail as (
    select
        state, provider_key,
        count(*) filter (where status in (select status from {{ ref('report_status_sets') }}
                                          where status_set_id = 'CLAIM_PAID')) as claims_paid_count,
        count(*) filter (where status in (select status from {{ ref('report_status_sets') }}
                                          where status_set_id = 'CLAIM_DENIED')) as claims_denied_count
    from {{ ref('mart_funded_reserve_claims') }}
    where has_requirement and is_filing_product_scope and closed_date between period_start and period_end
    group by all
)

select s.state, s.provider_key, s.claims_paid_count, d.claims_paid_count as detail_paid,
       s.claims_denied_count, d.claims_denied_count as detail_denied
from summary as s
left join detail as d using (state, provider_key)
where s.claims_paid_count <> coalesce(d.claims_paid_count, 0)
   or s.claims_denied_count <> coalesce(d.claims_denied_count, 0)
