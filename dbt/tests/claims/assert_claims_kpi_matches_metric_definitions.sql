-- Metric rule (docs/V3_DESIGN.md 3): mart_claims_kpi must agree with the metrics.yml definitions,
-- recomputed straight from the core facts. Returns one row per failed check.
with mart as (
    select
        sum(claim_count) as claim_count,
        sum(claims_reported) as claims_reported,
        sum(exposure_years) as exposure_years,
        sum(paid_amount) as paid_amount
    from {{ ref('mart_claims_kpi') }}
),

core as (
    select
        (select count(*) filter (where status_group <> 'denied') from {{ ref('dim_claim') }}) as claim_count,
        (select count(*) from {{ ref('dim_claim') }}) as claims_reported,
        (select sum(exposure_years) from {{ ref('fct_contract_monthly') }}) as exposure_years,
        (select sum(paid_amount) from {{ ref('fct_claim_payment_line') }}) as paid_amount
),

totals as (
    select 'claim_count' as check_name, m.claim_count::double as mart_value, c.claim_count::double as core_value
    from mart as m cross join core as c
    union all
    select 'claims_reported', m.claims_reported, c.claims_reported from mart as m cross join core as c
    union all
    select 'exposure_years', m.exposure_years, c.exposure_years from mart as m cross join core as c
    union all
    select 'paid_amount', m.paid_amount, c.paid_amount from mart as m cross join core as c
),

rows_ as (
    select 'row_formula' as check_name, 0.0 as mart_value, 0.0 as core_value
    from {{ ref('mart_claims_kpi') }}
    where abs(coalesce(claim_frequency, 0) - coalesce(claim_count / nullif(exposure_years, 0), 0)) > 1e-9
       or abs(coalesce(claim_severity, 0)
              - coalesce(paid_amount_on_paid_claims / nullif(paid_claim_count, 0), 0)) > 1e-6
       or abs(coalesce(denial_rate, 0) - coalesce(claims_denied / nullif(claims_reported, 0), 0)) > 1e-9
)

select * from totals where abs(coalesce(mart_value, 0) - coalesce(core_value, 0)) > 0.01
union all
select * from rows_
