-- Claims KPI cube. Grain: tenant x product subcategory x loss month.
-- Formulas follow dbt/models/metrics.yml and docs/METRICS.md (tested in dbt/tests/claims/):
--   claim_frequency = claim_count (non-denied, by loss month) / exposure_years
--   claim_severity  = paid_amount on paid claims / paid_claim_count   (open claims excluded)
--   denial_rate     = claims_denied / claims_reported
-- Basis: every count is by LOSS month. claims_reported includes claims with no matched contract
-- (no contract, or loss before the sale: claims_unmatched, carried separately); the V1 actuarial pages
-- (gold.mart_claims_trend) leave those out, so claims_reported - claims_unmatched ties to them
-- (dbt/tests/claims/assert_claims_reported_ties_across_pages.sql).
-- Early-claim windows (days from contract effective to loss) and the aging threshold come from
-- seed dealer_claim_kpi_rules; tests/claims checks the column list below matches the seed.
{% set windows = [30, 60, 90, 120, 150, 180] %}
with claims as (
    select
        tenant_key,
        subcategory_code,
        loss_month as month_start,
        count(*) as claims_reported,
        count(*) filter (where not is_contract_matched) as claims_unmatched,
        count(*) filter (where status_group = 'denied') as claims_denied,
        count(*) filter (where status_group <> 'denied') as claim_count,
        count(*) filter (where status_group = 'paid') as paid_claim_count,
        count(*) filter (where status_group = 'open') as open_claims,
        count(*) filter (where is_aged_open) as open_claims_aged_30_plus,
        {% for w in windows %}
        count(*) filter (where early_claim_window_days <= {{ w }}) as early_claims_{{ w }},
        {% endfor %}
        sum(paid_amount) filter (where status_group = 'paid') as paid_amount_on_paid_claims,
        sum(paid_amount) as paid_amount,
        sum(incurred_amount) as incurred_amount,
        sum(turnaround_days) filter (where status_group <> 'open') as turnaround_days_total,
        count(turnaround_days) filter (where status_group <> 'open') as turnaround_claims
    from {{ ref('mart_claim_summary') }}
    group by all
),

exposure as (
    select tenant_key, coalesce(subcategory_code, 'UNKNOWN') as subcategory_code,
        cast(month_start as date) as month_start, sum(exposure_years) as exposure_years
    from {{ ref('fct_contract_monthly') }}
    group by all
),

joined as (
    select
        coalesce(c.tenant_key, e.tenant_key) as tenant_key,
        coalesce(c.subcategory_code, e.subcategory_code) as subcategory_code,
        coalesce(c.month_start, e.month_start) as month_start,
        coalesce(e.exposure_years, 0) as exposure_years,
        coalesce(c.claims_reported, 0) as claims_reported,
        coalesce(c.claims_unmatched, 0) as claims_unmatched,
        coalesce(c.claims_denied, 0) as claims_denied,
        coalesce(c.claim_count, 0) as claim_count,
        coalesce(c.paid_claim_count, 0) as paid_claim_count,
        coalesce(c.open_claims, 0) as open_claims,
        coalesce(c.open_claims_aged_30_plus, 0) as open_claims_aged_30_plus,
        {% for w in windows %}
        coalesce(c.early_claims_{{ w }}, 0) as early_claims_{{ w }},
        {% endfor %}
        round(coalesce(c.paid_amount_on_paid_claims, 0), 2) as paid_amount_on_paid_claims,
        round(coalesce(c.paid_amount, 0), 2) as paid_amount,
        round(coalesce(c.incurred_amount, 0), 2) as incurred_amount,
        coalesce(c.turnaround_days_total, 0) as turnaround_days_total,
        coalesce(c.turnaround_claims, 0) as turnaround_claims
    from claims as c
    full outer join exposure as e using (tenant_key, subcategory_code, month_start)
)

select
    j.*,
    {{ yyyymm('j.month_start') }} as loss_month_key,
    {{ safe_divide('j.claim_count', 'j.exposure_years') }} as claim_frequency,
    {{ safe_divide('j.paid_amount_on_paid_claims', 'j.paid_claim_count') }} as claim_severity,
    {{ safe_divide('j.claims_denied', 'j.claims_reported') }} as denial_rate,
    {{ safe_divide('j.turnaround_days_total', 'j.turnaround_claims') }} as avg_turnaround_days,
    {{ audit_columns() }}
from joined as j
