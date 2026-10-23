-- Metric check (dbt/models/metrics.yml claim_paid_amount = sum of paid_amount on payment lines):
-- the market survey's claims-paid item, summed over its years, equals the same metric computed
-- straight from core.fct_claim_payment_line for the survey states, issued contracts and years.
with survey as (
    select state, sum(value) as paid
    from {{ ref('mart_market_survey') }}
    where item = 'CLAIMS_PAID_AMOUNT'
    group by 1
),

years as (
    select state, min(year_start) as from_date, max(year_end) as to_date
    from {{ ref('mart_market_survey') }}
    group by 1
),

metric as (
    select c.state, sum(p.paid_amount) as paid
    from {{ ref('fct_claim_payment_line') }} as p
    inner join {{ ref('mart_funded_reserve_contracts') }} as c using (contract_key)
    inner join years as y on y.state = c.state
    where cast(p.posted_at as date) between y.from_date and y.to_date
      and c.entered_date <= cast(p.posted_at as date)
      and c.status in (select status from {{ ref('report_status_sets') }} where status_set_id = 'WRITTEN')
      and c.category_code in ('VSC', 'ANCILLARY', 'SPECIALTY', 'LIMITED_WARRANTY', 'MAINTENANCE')
    group by 1
)

select s.state, s.paid as survey_paid, m.paid as metric_paid
from survey as s
left join metric as m using (state)
where abs(s.paid - coalesce(m.paid, 0)) > 0.01
